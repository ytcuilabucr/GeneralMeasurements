"""The program's own process priority and CPU cores.

Kept in `Instrument config/misc.toml` and applied when the Instrument Server
starts (and at once when changed in its Miscellaneous menu):

    [process]
    priority = "high"        # normal / above_normal / high
    cores = [2, 3]           # logical processors to run on; empty = all

Priority and cores are set with the Windows API (kernel32). Elsewhere only the
cores can be set (`os.sched_setaffinity`), where the OS offers it.

Realtime is not offered: it can starve the mouse, the keyboard and the
USB/VISA drivers the instruments depend on. The cores are not reserved: other
programs still run on them, so pinning to a single core makes stalls more
likely, not less.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterable

PRIORITIES = ("normal", "above_normal", "high")
LABELS = {"normal": "Normal", "above_normal": "Above normal", "high": "High"}
DEFAULT_PRIORITY = "high"
PRIORITY_KEY, CORES_KEY = "process/priority", "process/cores"
_CLASSES = {"normal": 0x20, "above_normal": 0x8000, "high": 0x80}   # *_PRIORITY_CLASS
_NAMES = {**{v: k for k, v in _CLASSES.items()}, 0x40: "idle", 0x4000: "below_normal",
          0x100: "realtime"}


def _kernel32():
    """kernel32 with the calls used here typed, or None off Windows."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k.GetPriorityClass.restype = wintypes.DWORD
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.SetPriorityClass.restype = wintypes.BOOL
    mask_p = ctypes.POINTER(ctypes.c_size_t)                          # DWORD_PTR*
    k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, mask_p, mask_p]
    k.GetProcessAffinityMask.restype = wintypes.BOOL
    k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    k.SetProcessAffinityMask.restype = wintypes.BOOL
    return k


def _win_error() -> OSError:
    """The last Windows error (ctypes' Windows-only helpers, typed for any platform)."""
    return getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())


def _bits(mask: int) -> list[int]:
    return [i for i in range(mask.bit_length()) if mask >> i & 1]


def _masks(k) -> tuple[int, int]:
    """(this process's mask, the system's mask)."""
    proc, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(proc),
                                    ctypes.byref(system)):
        raise _win_error()
    return proc.value, system.value


def all_cores() -> list[int]:
    """The logical processors this computer offers."""
    k = _kernel32()
    if k is not None:
        return _bits(_masks(k)[1])
    return list(range(os.cpu_count() or 1))


def cores() -> list[int]:
    """The logical processors this process may run on now."""
    k = _kernel32()
    if k is not None:
        return _bits(_masks(k)[0])
    if hasattr(os, "sched_getaffinity"):
        return sorted(getattr(os, "sched_getaffinity")(0))     # not on Windows
    return all_cores()


def priority() -> str | None:
    """This process's priority ("normal", "high", ...); None where it can't be read."""
    k = _kernel32()
    if k is None:
        return None
    return _NAMES.get(k.GetPriorityClass(k.GetCurrentProcess()))


def can_set_priority() -> bool:
    return sys.platform == "win32"


def can_set_cores() -> bool:
    return sys.platform == "win32" or hasattr(os, "sched_setaffinity")


def check(prio: str, chosen: Iterable[int]) -> list[int]:
    """The settings, checked: the cores (sorted; empty = all) for `prio`."""
    if prio not in PRIORITIES:
        raise ValueError(f"process priority {prio!r}: one of {', '.join(PRIORITIES)}")
    chosen = sorted(set(chosen))
    if any(not isinstance(c, int) or isinstance(c, bool) for c in chosen):
        raise ValueError(f"process cores {chosen}: whole numbers")
    missing = [c for c in chosen if c not in all_cores()]
    if missing:
        raise ValueError(f"process cores {missing}: this computer has cores "
                         f"0-{max(all_cores())}")
    return chosen


def set_priority(prio: str) -> None:
    check(prio, [])
    k = _kernel32()
    if k is None:
        raise OSError("process priority can only be set on Windows")
    if not k.SetPriorityClass(k.GetCurrentProcess(), _CLASSES[prio]):
        raise _win_error()


def set_cores(chosen: Iterable[int]) -> None:
    """Run on these logical processors only; empty = all of them."""
    chosen = check(DEFAULT_PRIORITY, chosen) or all_cores()
    k = _kernel32()
    if k is not None:
        if not k.SetProcessAffinityMask(k.GetCurrentProcess(), sum(1 << c for c in chosen)):
            raise _win_error()
    elif hasattr(os, "sched_setaffinity"):
        getattr(os, "sched_setaffinity")(0, chosen)
    else:
        raise OSError("CPU cores can't be chosen on this system")


def describe() -> str:
    """This process now, e.g. "priority High, cores 2, 3 (of 0-7)"."""
    p = priority()
    now, every = cores(), all_cores()
    used = "all cores" if now == every else f"cores {', '.join(map(str, now))}"
    text = f"{used} (of 0-{max(every)})"
    return text if p is None else f"priority {LABELS.get(p, p)}, {text}"


def apply(prio: str, chosen: Iterable[int]) -> str:
    """Set both; returns `describe()` afterwards. Raises ValueError or OSError,
    having set what it could."""
    chosen = check(prio, chosen)
    errors = []
    if can_set_priority():
        try:
            set_priority(prio)
        except OSError as e:
            errors.append(f"priority: {e}")
    if can_set_cores():
        try:
            set_cores(chosen)
        except OSError as e:
            errors.append(f"cores: {e}")
    elif chosen:
        errors.append("cores: can't be chosen on this system")
    if errors:
        raise OSError("; ".join(errors))
    return describe()


def from_settings(settings) -> tuple[str, list[int]]:
    """(priority, cores) from the app settings (`value(key, default)`), with defaults
    for anything missing or not understood. Cores this computer doesn't have (a
    file copied from another PC) are left out."""
    prio = settings.value(PRIORITY_KEY, DEFAULT_PRIORITY)
    prio = prio if prio in PRIORITIES else DEFAULT_PRIORITY
    raw = settings.value(CORES_KEY, [])
    chosen = raw if isinstance(raw, list) else []
    every = all_cores()
    return prio, [c for c in chosen
                  if isinstance(c, int) and not isinstance(c, bool) and c in every]
