"""Cryomagnetics LM-510 helium level panel.

Laid out after the legacy HeliumMonitor.vi: a dewar gauge, the level, the
boil-off rate from a linear fit (in/day, as the VI showed), the time left
until the meter's low mark, Sample/Hold vs Continuous mode, a measure-now
button, and the level chart with Auto / Last 30 min / Last 24 h ranges.

Polling only fetches the last measured level; the meter itself decides when
to measure (its interval in Sample/Hold mode), because each measurement boils
off helium. 'Measure now' starts one on request.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtWidgets

from ..panels import Panel
from ..widgets import Card, CsvLog, DewarGauge, TimeChart, accent_style, duration, slope_per_s

ACCENT = "#17a589"
FITS = {"1 h": 3600.0, "6 h": 6 * 3600.0, "24 h": 86400.0}


class LM510Panel(Panel):
    DEFAULT_INTERVAL = 10.0
    MAX_INTERVAL = 600.0

    def build_layout(self, root):
        self.setStyleSheet(accent_style(ACCENT))
        self.title.setObjectName("panelTitle")
        root.addLayout(self.head)

        top = QtWidgets.QHBoxLayout()
        self.dewar = DewarGauge(self.cfg.units)
        top.addWidget(self.dewar)
        right = QtWidgets.QVBoxLayout()
        cards = QtWidgets.QHBoxLayout()
        self.level = Card("Helium level", self.cfg.units, size=32)
        self.boil = Card("Boil-off", f"{self.cfg.units}/day", size=20)
        self.fit = QtWidgets.QComboBox()
        self.fit.addItems(list(FITS))
        self.fit.setCurrentText("6 h")
        self.fit.setToolTip("Linear fit over this much history")
        self.boil.extra.addWidget(QtWidgets.QLabel("fit over"))
        self.boil.extra.addWidget(self.fit)
        self.left = Card("Until low mark", size=20)
        for c in (self.level, self.boil, self.left):
            cards.addWidget(c)
        right.addLayout(cards)
        right.addWidget(self.info)

        self.mode_box = QtWidgets.QGroupBox("Measurement")
        f = QtWidgets.QFormLayout(self.mode_box)
        modes = QtWidgets.QHBoxLayout()
        self.mode_sh = QtWidgets.QRadioButton("Sample/Hold")
        self.mode_c = QtWidgets.QRadioButton("Continuous")
        self.mode_c.setToolTip("Continuous keeps the sensor heated: higher boil-off")
        for b in (self.mode_sh, self.mode_c):
            modes.addWidget(b)
            b.clicked.connect(self._mode)
        modes.addStretch()
        f.addRow("Mode", modes)
        intv = QtWidgets.QHBoxLayout()
        self.meter_interval = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0.1,
                                                       maximum=24 * 60, value=60,
                                                       suffix=" min")
        set_i = QtWidgets.QPushButton("Set")
        set_i.clicked.connect(self._interval)
        intv.addWidget(self.meter_interval)
        intv.addWidget(set_i)
        intv.addStretch()
        f.addRow("Meter interval", intv)
        now = QtWidgets.QPushButton("Measure now")
        now.setToolTip("Start one measurement (heats the sensor briefly)")
        now.clicked.connect(self._measure)
        self.last = QtWidgets.QLabel("—")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(now)
        row.addWidget(self.last)
        row.addStretch()
        f.addRow("", row)
        right.addWidget(self.mode_box)
        right.addStretch()
        top.addLayout(right, stretch=1)
        root.addLayout(top)

        self.chart = TimeChart("helium level", self.cfg.units, {"level": "#48c9b0"})
        root.addWidget(self.chart, stretch=1)
        self.log = CsvLog(self.name)
        root.addWidget(self.log)
        root.addLayout(self.foot)
        self.history: deque[tuple[float, float]] = deque(maxlen=20000)
        self._low: float | None = None
        self._last_value: float | None = None

    def build_readouts(self):
        self.history.clear()

    def _set_enabled(self, on):
        super()._set_enabled(on)
        self.mode_box.setEnabled(on)

    def safe_tip(self):
        return "Nothing to make safe: a level meter only measures"

    async def attach(self):
        try:
            await self.dev.configure()          # read-only checks
            problem = None
        except TimeoutError as e:               # silent meter: say so, don't go on
            raise TimeoutError(f"{e}. No reply from the meter: check that it is on, the "
                               "serial cable to the LAN adapter, and its baud rate") from e
        except Exception as e:  # noqa: BLE001
            problem = str(e)
        return problem, await self.dev.snapshot()

    def after_attach(self, result):
        problem, snap = result
        self._low = snap["low"] if math.isfinite(snap["low"]) else None
        full = None
        try:
            full = float(snap["length"].split()[0])
        except (ValueError, IndexError):
            pass
        high = snap["high"] if math.isfinite(snap["high"]) else None
        self.dewar.set(full=full or high, low=self._low, high=high)
        (self.mode_c if snap["mode"] == "continuous" else self.mode_sh).setChecked(True)
        self.meter_interval.setValue(snap["interval_s"] / 60)
        self.info.setText(f"{snap['idn']}<br>channel {snap['channel']}; sensor "
                          f"{snap['length']}; low {snap['low']:g}, high {snap['high']:g} "
                          f"{self.cfg.units}; mode {snap['mode']}, every "
                          f"{duration(snap['interval_s'])}")
        if problem:
            self.station.log("warn", f"{self.name}: {problem}")
            self.info.setText(self.info.text() + f"<br><span style='color:#c0392b'>{problem}"
                                                 "</span>")

    def show_data(self, data):
        if not data:
            return
        now, v, u = time.time(), data[self.name], self.cfg.units
        self.history.append((now, v))
        if v != self._last_value:              # a new measurement came in
            self._last_value = v
            self.last.setText(f"last change {time.strftime('%H:%M:%S')}")
        self.dewar.set(level=v)
        self.level.set(f"{v:.1f} {u}")
        self.chart.add("level", v, now)
        k = slope_per_s(self.history, FITS[self.fit.currentText()])
        if k is None:
            self.boil.set("—", "needs more history")
            self.left.set("—")
        else:
            self.boil.set(f"{-k * 86400:.2f} {u}/day", f"{-k * 60:.4f} {u}/min")
            if self._low is not None and k < 0:
                self.left.set(duration((v - self._low) / -k), f"to {self._low:g} {u}")
            else:
                self.left.set("—", "level not falling")
        self.log.write({"level": v, "units": u})

    def _mode(self):
        mode = "continuous" if self.mode_c.isChecked() else "sample/hold"
        self.call(self.dev.set_mode(mode), f"mode {mode}")

    def _interval(self):
        s = self.meter_interval.value() * 60
        self.call(self.dev.set_interval(s), f"meter interval {duration(s)}")

    def _measure(self):
        self.call(self.dev.measure(), "measure now",
                  lambda _: QtCore.QTimer.singleShot(5000, self.poll))
