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

from ..form_types import Lakeshore335Form
from ..panels import Panel
from ..widgets import CardLabels, is_stable, slope_per_s

ACCENT = "#d35400"                      # also in lakeshore_335.ui's style sheet
RANGES = ("Off", "Low", "Med", "High")


class LakeShore335Panel(Panel, Lakeshore335Form):
    UI = "lakeshore_335"

    def setup_ui(self):
        self.card = {inp: CardLabels(self, f"card{inp}") for inp in ("A", "B")}
        self.stable_led = {"A": self.stable_A, "B": self.stable_B}
        self.rate_label = {"A": self.rate_A, "B": self.rate_B}
        self.sp_card, self.heat_card = CardLabels(self, "cardSp"), CardLabels(self, "cardHeat")
        self.btn_ramp.clicked.connect(self._ramp)
        self.btn_hold.clicked.connect(lambda: self.call(self.dev.hold(), "hold setpoint"))
        self._ramp_controls = [self.sp, self.rate, self.btn_ramp]
        self.range_group = QtWidgets.QButtonGroup(self)
        self.range_group.setExclusive(True)
        for i in range(len(RANGES)):
            self.range_group.addButton(getattr(self, f"range_{i}"), i)
        self.range_group.idClicked.connect(self._range)
        self.tol.setValue(self.cfg.tolerance)         # what 'stable' and 'rate' mean
        self.stable_win.setValue(self.cfg.stable_s)
        self.log.name = self.name
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
