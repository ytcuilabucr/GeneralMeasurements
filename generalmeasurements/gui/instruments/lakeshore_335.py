"""Lake Shore 335 temperature controller panel.

Laid out after the legacy TemperatureControl.vi: both sensor inputs as big
readouts with a 'stable' light and a rate (K/min, linear fit), the temperature
history with the setpoint, setpoint ramp control, heater range and output, PID
for reference, and CSV logging.

PID, the control input and the heater limits are shown, never written (the
driver leaves them to the operator; the VI's Set PID is deliberately absent).
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtWidgets

from ..panels import Panel, SIEdit
from ..widgets import Card, CsvLog, Led, TimeChart, accent_style, is_stable, slope_per_s

ACCENT = "#d35400"
RANGES = ("Off", "Low", "Med", "High")


class LakeShore335Panel(Panel):
    def build_layout(self, root):
        self.setStyleSheet(accent_style(ACCENT))
        self.title.setObjectName("panelTitle")
        root.addLayout(self.head)

        cards = QtWidgets.QHBoxLayout()
        self.card = {}
        self.stable_led, self.rate_label = {}, {}
        for inp in ("A", "B"):
            c = Card(f"Temp {inp}", "K", size=22)
            self.stable_led[inp] = Led("stable")
            self.rate_label[inp] = QtWidgets.QLabel("rate —")
            c.extra.addWidget(self.stable_led[inp])
            c.extra.addStretch()
            c.extra.addWidget(self.rate_label[inp])
            self.card[inp] = c
            cards.addWidget(c, stretch=2)
        self.sp_card = Card("Setpoint", "K", size=15)
        cards.addWidget(self.sp_card, stretch=1)
        heat = Card("Heater output", "%", size=15)
        self.heat_bar = QtWidgets.QProgressBar()
        self.heat_bar.setRange(0, 1000)
        self.heat_bar.setTextVisible(False)
        self.heat_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {ACCENT}; }}")
        heat.extra.addWidget(self.heat_bar)
        self.heat_card = heat
        cards.addWidget(heat, stretch=1)
        root.addLayout(cards)
        root.addWidget(self.info)

        self.chart = TimeChart("temperature", "K", {"A": "#e67e22", "B": "#5dade2",
                                                    "setpoint": "#f4d03f"})
        root.addWidget(self.chart, stretch=1)

        low = QtWidgets.QHBoxLayout()
        self.sp_box = QtWidgets.QGroupBox("Setpoint ramp")
        f = QtWidgets.QFormLayout(self.sp_box)
        self.ramp_state = QtWidgets.QLabel("—")
        f.addRow("Status", self.ramp_state)
        self.sp = SIEdit(unit="K")
        self.rate = SIEdit(unit="K/min")
        row = QtWidgets.QHBoxLayout()
        for w in (self.sp, QtWidgets.QLabel("K  at"), self.rate, QtWidgets.QLabel("K/min")):
            row.addWidget(w)
        row.addStretch()
        f.addRow("Setpoint", row)
        go = QtWidgets.QPushButton("Ramp to setpoint")
        go.clicked.connect(self._ramp)
        hold = QtWidgets.QPushButton("Hold")
        hold.setToolTip("Stop the setpoint ramp where it is now")
        hold.clicked.connect(lambda: self.call(self.dev.hold(), "hold setpoint"))
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(go)
        row2.addWidget(hold)
        row2.addStretch()
        f.addRow("", row2)
        self._ramp_controls = [self.sp, self.rate, go]
        low.addWidget(self.sp_box, stretch=3)

        self.heat_box = QtWidgets.QGroupBox("Heater")
        f2 = QtWidgets.QFormLayout(self.heat_box)
        rng = QtWidgets.QHBoxLayout()
        self.range_group = QtWidgets.QButtonGroup(self)
        self.range_group.setExclusive(True)
        for i, text in enumerate(RANGES):
            b = QtWidgets.QPushButton(text)
            b.setCheckable(True)
            self.range_group.addButton(b, i)
            rng.addWidget(b)
        self.range_group.idClicked.connect(self._range)
        f2.addRow("Range", rng)
        self.pid = QtWidgets.QLabel("—")
        self.pid.setToolTip("Set PID on the front panel; this program never writes it")
        f2.addRow("PID (read only)", self.pid)
        low.addWidget(self.heat_box, stretch=2)

        root.addLayout(low)

        mon = QtWidgets.QHBoxLayout()           # what 'stable' and 'rate' mean
        self.tol = QtWidgets.QDoubleSpinBox(decimals=3, minimum=0.001, maximum=10.0,
                                            singleStep=0.01, value=self.cfg.tolerance,
                                            suffix=" K")
        self.stable_win = QtWidgets.QDoubleSpinBox(decimals=0, minimum=5, maximum=3600,
                                                   value=self.cfg.stable_s, suffix=" s")
        self.fit = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0.5, maximum=120, value=5,
                                            suffix=" min")
        for w in (QtWidgets.QLabel("Stable within ±"), self.tol, QtWidgets.QLabel("for"),
                  self.stable_win, QtWidgets.QLabel("   Rate fit over"), self.fit):
            mon.addWidget(w)
        mon.addStretch()
        root.addLayout(mon)

        self.log = CsvLog(self.name)
        root.addWidget(self.log)
        root.addLayout(self.foot)
        self.history = {"A": deque(maxlen=20000), "B": deque(maxlen=20000)}

    def build_readouts(self):
        for inp in ("A", "B"):
            ctrl = " (control)" if inp == self.dev.input else ""
            self.card[inp].caption.setText(f"<b>Temp {inp}</b>{ctrl}")
        self.history["A"].clear()
        self.history["B"].clear()

    def _set_enabled(self, on):
        super()._set_enabled(on)
        for g in (self.sp_box, self.heat_box):
            g.setEnabled(on)

    def safe_tip(self):
        return "hold: freeze the setpoint ramp; off: heater range 0 (station safe_action)"

    async def attach(self):
        try:
            await self.dev.configure()          # read-only checks
            problem = None
        except Exception as e:  # noqa: BLE001
            problem = str(e)
        return problem, await self.dev.snapshot()

    def after_attach(self, result):
        problem, snap = result
        d = self.dev
        self.rate.setText(f"{d.max_rate * 60:.3g}")
        try:
            self.sp.setText(f"{float(snap['setpoint']):g}")
        except ValueError:
            pass
        self.pid.setText(snap["pid"].replace(",", ",  "))
        self.info.setText(f"Loop {d.loop} on input {d.input}; limits {d.limits[0]:g}…"
                          f"{d.limits[1]:g} K; max {d.max_rate * 60:.3g} K/min")
        if problem:
            self.station.log("warn", f"{self.name}: {problem}. Setpoint ramps disabled; "
                                     "readings, Hold and heater range still work.")
            self.info.setText(self.info.text() + f"<br><span style='color:#c0392b'>{problem}"
                                                 "</span>")
        for w in self._ramp_controls:
            w.setEnabled(problem is None)

    async def read_coro(self):
        d = self.dev
        data = await d.read()
        other = "B" if d.input == "A" else "A"
        data["A" if d.input == "A" else "B"] = data[self.name]
        data[other] = await d.read_input(other)
        data["_ramping"] = not await d.ramp_done()
        data["_range"] = await d.get_heater_range()
        data["_ramp"] = await d.ramp_rate()
        return data

    def show_data(self, data):
        if not data:
            return
        now = time.time()
        sp, heater = data[f"{self.name}.setpoint"], data[f"{self.name}.heater"]
        for inp in ("A", "B"):
            v = data[inp]
            self.history[inp].append((now, v))
            self.card[inp].set("fault" if math.isnan(v) else f"{v:.3f} K")
            self.chart.add(inp, v, now)
            s = is_stable(self.history[inp], self.stable_win.value(), self.tol.value())
            self.stable_led[inp].set({True: "on", False: "warn", None: "off"}[s],
                                     {True: "stable", False: "not stable",
                                      None: "stable?"}[s])
            k = slope_per_s(self.history[inp], self.fit.value() * 60)
            self.rate_label[inp].setText("rate —" if k is None else f"{k * 60:+.3f} K/min")
        self.chart.add("setpoint", sp, now)
        ramp_on, rate = data["_ramp"]
        self.sp_card.set(f"{sp:.3f} K", f"ramp {'on' if ramp_on else 'off'}, "
                                        f"{rate * 60:.3g} K/min")
        self.ramp_state.setText(f"<b style='color:{ACCENT}'>ramping</b>" if data["_ramping"]
                                else "idle (at setpoint)")
        self.heat_card.set(f"{heater:.1f} %", f"range {RANGES[data['_range']]}")
        self.heat_bar.setValue(round(heater * 10))
        btn = self.range_group.button(data["_range"])
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        self.log.write({"T_A": data["A"], "T_B": data["B"], "setpoint": sp,
                        "heater_pct": heater, "range": data["_range"]})

    def _ramp(self):
        sp = self.value_of(self.sp, "setpoint")
        rate = self.value_of(self.rate, "rate")
        if sp is not None and rate is not None:
            self.call(self.dev.start_ramp(sp, rate / 60),
                      f"ramp setpoint to {sp:g} K at {rate:g} K/min")

    def _range(self, r: int):
        if r and not self.confirm(f"Set loop {self.dev.loop} heater range to {RANGES[r]}?"):
            self.poll()                          # put the buttons back to the real range
            return
        self.call(self.dev.set_heater_range(r), f"heater range {RANGES[r]}",
                  lambda _: self.poll())
