"""Lake Shore 625 superconducting magnet supply panel.

Laid out after the legacy MagnetControl.vi: a large field / current readout,
the output voltage, a bar over the station limits with the target, ramp
progress with an ETA, 'current stable' and switch-heater lights, ramp control,
the trend, and CSV logging.

The VI's persistent-switch sequencing (P.S. heater on/off, auto zero, ramp
segments) is not here: the driver never writes the switch heater, limits or
segments. The heater state is shown read only; with `persistent_switch = true`
in the station file, ramps are refused unless the heater is on.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6 import QtWidgets

from ..panels import Panel, SIEdit, parse_si
from ..widgets import Card, CsvLog, FieldBar, Led, TimeChart, accent_style, duration

ACCENT = "#2e86c1"


class LakeShore625Panel(Panel):
    DEFAULT_INTERVAL = 0.5

    def build_layout(self, root):
        self.setStyleSheet(accent_style(ACCENT))
        self.title.setObjectName("panelTitle")
        root.addLayout(self.head)

        top = QtWidgets.QHBoxLayout()
        self.main = Card("Magnet", size=34)
        top.addWidget(self.main, stretch=3)
        side = QtWidgets.QVBoxLayout()
        self.i_card = Card("Output current", "A", size=16)
        self.v_card = Card("Output voltage", "V", size=16)
        side.addWidget(self.i_card)
        side.addWidget(self.v_card)
        top.addLayout(side, stretch=2)
        lights = QtWidgets.QGroupBox("Status")
        lv = QtWidgets.QVBoxLayout(lights)
        self.led_ramp = Led("—")
        self.led_stable = Led("current stable")
        self.led_psh = Led("P.S. heater")
        self.led_psh.setToolTip("Persistent-switch heater (read only)")
        self.led_err = Led("supply errors")
        for w in (self.led_ramp, self.led_stable, self.led_psh, self.led_err):
            lv.addWidget(w)
        lv.addStretch()
        top.addWidget(lights, stretch=2)
        root.addLayout(top)

        self.bar = FieldBar()
        root.addWidget(self.bar)
        prog = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("")
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {ACCENT}; }}")
        self.eta = QtWidgets.QLabel("—")
        prog.addWidget(QtWidgets.QLabel("Ramp progress"))
        prog.addWidget(self.progress, stretch=1)
        prog.addWidget(self.eta)
        root.addLayout(prog)
        root.addWidget(self.info)

        self.chart = TimeChart("magnet", "", {"output": "#5dade2", "target": "#e67e22"})
        root.addWidget(self.chart, stretch=1)

        low = QtWidgets.QHBoxLayout()
        self.ramp_box = QtWidgets.QGroupBox("Ramp")
        f = QtWidgets.QFormLayout(self.ramp_box)
        self.target = SIEdit()
        self.rate = SIEdit()
        self.unit_t, self.unit_r = QtWidgets.QLabel(""), QtWidgets.QLabel("")
        row = QtWidgets.QHBoxLayout()
        for w in (self.target, self.unit_t, QtWidgets.QLabel("at"), self.rate, self.unit_r):
            row.addWidget(w)
        row.addStretch()
        f.addRow("Target", row)
        go = QtWidgets.QPushButton("Ramp…")
        go.clicked.connect(self._ramp)
        zero = QtWidgets.QPushButton("Ramp to zero…")
        zero.clicked.connect(self._zero)
        hold = QtWidgets.QPushButton("Hold")
        hold.setToolTip("Hold the present current (stops a ramp)")
        hold.clicked.connect(lambda: self.call(self.dev.hold(), "hold", lambda _: self.poll()))
        row2 = QtWidgets.QHBoxLayout()
        for w in (go, zero, hold):
            row2.addWidget(w)
        row2.addStretch()
        f.addRow("", row2)
        self._ramp_controls = [self.target, self.rate, go, zero]
        low.addWidget(self.ramp_box, stretch=3)

        self.mon_box = QtWidgets.QGroupBox("Monitor")
        f2 = QtWidgets.QFormLayout(self.mon_box)
        self.buffer = QtWidgets.QSpinBox(minimum=3, maximum=600, value=10, suffix=" samples")
        self.tol = QtWidgets.QDoubleSpinBox(decimals=4, minimum=0.0001, maximum=1.0,
                                            singleStep=0.001, value=self.cfg.tolerance_a,
                                            suffix=" A")
        f2.addRow("Stable buffer", self.buffer)
        f2.addRow("within ±", self.tol)
        low.addWidget(self.mon_box, stretch=2)
        root.addLayout(low)

        self.log = CsvLog(self.name)
        root.addWidget(self.log)
        root.addLayout(self.foot)
        self.history: deque[tuple[float, float]] = deque(maxlen=20000)
        self._start: float | None = None
        self._rate_now: float | None = None

    def build_readouts(self):
        d = self.dev
        self.main.caption.setText(f"<b>{'Field' if d.k else 'Current'}</b>")
        self.unit_t.setText(d.units)
        self.unit_r.setText(f"{d.units}/s")
        self.bar.unit = d.units
        self.chart.plot.setLabel("left", "field" if d.k else "current", d.units)
        self.i_card.setVisible(bool(d.k))
        self.history.clear()

    def _set_enabled(self, on):
        super()._set_enabled(on)
        self.ramp_box.setEnabled(on)

    def safe_tip(self):
        return "Hold the present current. Never ramps the magnet down."

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
        self.rate.setText(f"{d.max_rate:g}")
        try:
            self._rate_now = float(snap["rate"]) * (d.k or 1.0)
        except ValueError:
            self._rate_now = d.max_rate
        self.bar.set(limits=tuple(d.limits), target=d.target)
        errs = snap["erst"].strip()
        self.led_err.set("on" if set(errs) <= set("0,") else "bad",
                         "no supply errors" if set(errs) <= set("0,") else f"errors {errs}")
        self.info.setText(f"Limits {d.limits[0]:g}…{d.limits[1]:g} {d.units}; max "
                          f"{d.max_rate:g} {d.units}/s; supply LIMIT {snap['limit']}; "
                          f"SETI {snap['seti']}; persistent switch "
                          f"{'required on' if d.persistent_switch else 'not used'}")
        if problem:
            self.station.log("warn", f"{self.name}: {problem}. Ramps disabled.")
            self.info.setText(self.info.text() + f"<br><span style='color:#c0392b'>{problem}"
                                                 "</span>")
        for w in self._ramp_controls:
            w.setEnabled(problem is None)

    async def read_coro(self):
        data = await self.dev.read()
        data["_done"] = await self.dev.ramp_done()
        data["_psh"] = await self.dev.switch_heater_on()
        return data

    def show_data(self, data):
        if not data:
            return
        d, n, now = self.dev, self.name, time.time()
        x = data[n]
        i = data.get(f"{n}.i", x)
        self.history.append((now, i))
        self.main.set(f"{x:+.4f} {d.units}", f"target {d.target:+.4f} {d.units}")
        self.i_card.set(f"{i:+.4f} A")
        self.v_card.set(f"{data[f'{n}.v']:+.4f} V")
        self.bar.set(value=x, target=d.target)
        self.chart.add("output", x, now)
        self.chart.add("target", d.target, now)
        done = data["_done"]
        self.led_ramp.set("on" if done else "busy", "at target" if done else "ramping")
        buf = [y for _, y in list(self.history)[-self.buffer.value():]]
        s = None
        if len(buf) >= self.buffer.value():
            mean = sum(buf) / len(buf)
            s = max(abs(y - mean) for y in buf) <= self.tol.value()
        self.led_stable.set({True: "on", False: "warn", None: "off"}[s],
                            {True: "current stable", False: "current changing",
                             None: "current stable?"}[s])
        self.led_psh.set("warn" if data["_psh"] else "off",
                         "P.S. heater ON" if data["_psh"] else "P.S. heater off")
        # progress from where the ramp started (or from zero) to the target
        start = self._start if self._start is not None else 0.0
        span = d.target - start
        frac = 1.0 if done or abs(span) < 1e-12 else max(0.0, min(1.0, (x - start) / span))
        self.progress.setValue(round(frac * 1000))
        rate = self._rate_now or d.max_rate
        self.eta.setText("done" if done else f"ETA {duration(abs(d.target - x) / rate)}")
        if done:
            self._start = None
        self.log.write({"output": x, "current_A": i, "voltage_V": data[f"{n}.v"],
                        "target": d.target, "psh": int(data["_psh"])})

    def _present(self) -> float | None:
        try:
            return parse_si(self.main.value.text())
        except ValueError:
            return None

    def _go(self, target: float, rate: float, what: str) -> None:
        d = self.dev
        here = self._present()
        est = "?" if here is None else duration(abs(target - here) / rate)
        now = "?" if here is None else f"{here:+.4f}"
        if not self.confirm(f"{what}: ramp the magnet from {now} to {target:g} {d.units} at "
                            f"{rate:g} {d.units}/s (about {est})?"):
            return

        def started(_):
            self._start, self._rate_now = here, rate
            self.poll()
        self.call(d.start_ramp(target, rate), f"ramp to {target:g} {d.units} at "
                                              f"{rate:g} {d.units}/s", started)

    def _ramp(self):
        target = self.value_of(self.target, "target")
        rate = self.value_of(self.rate, "rate")
        if target is not None and rate is not None:
            self._go(target, rate, "Ramp")

    def _zero(self):
        rate = self.value_of(self.rate, "rate")
        if rate is not None:
            self._go(0.0, rate, "Ramp to zero")
