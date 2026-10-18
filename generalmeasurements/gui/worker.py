"""One asyncio loop in a background thread, driven from the Qt thread.

Every device call from the GUI goes through `AsyncWorker.submit()`, so all
instrument sessions live on one loop (the single-owner rule) and the window
never blocks on I/O. Callbacks run back on the Qt thread via a queued signal.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6 import QtCore


class _Bridge(QtCore.QObject):
    done = QtCore.Signal(object, object, object, object)   # on_done, on_error, result, exc

    def __init__(self):
        super().__init__()
        # emitted from the loop thread; queued so the slot runs on this object's (Qt) thread
        self.done.connect(self._deliver, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.Slot(object, object, object, object)
    def _deliver(self, on_done, on_error, result, exc):
        if exc is None:
            if on_done is not None:
                on_done(result)
        elif on_error is not None:
            on_error(exc)


class AsyncWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="generalmeasurements-io",
                                        daemon=True)
        self._thread.start()
        self._bridge = _Bridge()               # create on the Qt thread
        self._stopped = False

    def submit(self, coro: Coroutine[Any, Any, Any],
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None,
               ) -> concurrent.futures.Future:
        if self._stopped:                      # e.g. a poll timer firing while closing
            coro.close()
            fut: concurrent.futures.Future = concurrent.futures.Future()
            fut.cancel()
            return fut
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def finished(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                self._bridge.done.emit(on_done, on_error, None, asyncio.CancelledError())
            elif (exc := f.exception()) is not None:
                self._bridge.done.emit(on_done, on_error, None, exc)
            else:
                self._bridge.done.emit(on_done, on_error, f.result(), None)
        fut.add_done_callback(finished)
        return fut

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float = 30.0) -> Any:
        """Blocking call, for shutdown paths only."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self._stopped = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2)
