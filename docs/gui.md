# Instrument control windows

PySide6 with the Qt Advanced Docking System (PySide6-QtAds).

```
pip install -e .[gui]
gm gui --station station.lab.toml     # or station.sim.toml to try it offline
gm gui --station station.lab.toml --reset-layout
```

## Windows

- **Station window**: Connect all, Disconnect all, All safe state, Show all
  windows, Reset layout. It has one button per instrument window, with a status
  dot for each instrument (green connected, amber connecting, red error), and
  the log. Closing it closes the GUI.
- **One window per instrument type**. Instruments of the same type are docks in
  the same window:

  | Window | Drivers |
  | --- | --- |
  | Keithley | `keithley_24xx`, `keithley_2000` |
  | Temperature | `lakeshore_335` |
  | Magnet | `lakeshore_625`, `sim_magnet` |
  | Helium level | `lm510` |
  | RF source | `gigatronics_2420b` |
  | Lock-in | `sr830`, `sr850`, `sim_lockin` |
  | Sources | `sim_source` |

  Put `window = "..."` on a device in the station TOML to move it into another
  window, or into a new one. Each window's toolbar has Connect, Disconnect and
  Safe state for its own instruments, plus Station to bring the station window
  forward. Messages about an instrument also appear in its window's status bar.
  Closing an instrument window only hides it: its sessions stay open, and the
  button in the station window shows it again.
- **Inside a window** the docks start as tabs. Drag a tab to an edge to put
  two instruments side by side or one above the other. Drag it out of the
  window, or use the undock button, to float it. Docks can't be closed, so an
  instrument can't get lost.
- **Layout** (window positions, sizes, splits, floating docks, and which windows
  are open) is saved per station when the GUI closes, in
  `%APPDATA%\GeneralMeasurements\gui.ini`, and restored next time. A window
  whose instruments changed in the TOML starts fresh. **Reset layout** (or
  `--reset-layout`) tabs everything back and forgets the saved layout.
  The toolbar button keeps open monitor and chart windows; `--reset-layout`
  starts without them.

## Monitor and chart windows

Use these to show the same instrument in several places at once, for example
the magnet field as a big number on one screen and a chart of field and
temperature on another.

- **New monitor…** (station toolbar): pick fields and get a window of big
  numbers with units.
  - For each field you can optionally set amber and red thresholds, below or
    above. The number turns amber or red outside them.
- **New chart…**: pick fields and get a live wall-clock chart, with Display
  range (All / 30 min / 2 h / 24 h) and Clear.
  - Several instruments can share one chart. A second unit gets the right-hand
    y-axis, so a chart takes at most two units.
- The field list comes from the connected instruments' `describe()`, for
  example `magnet`, `magnet.v`, `temp`, `temp.setpoint`, `helium`, `lockin.x`.
  **Fields…** in a view's toolbar changes what it shows.
- Any number of each can be open at once. They are part of the saved layout,
  including their fields and thresholds. Closing one deletes it.
- **Read only.** Views never poll, write or open a session. Every value comes
  from the instrument panels' own polls, through the station's data hub, so
  opening more views adds no instrument traffic. Each instrument is still read
  once per poll interval of its panel, and only while that panel is connected
  with **Live** on (or on **Read now**). The station GUI stays the only owner
  of the sessions.
- When an instrument disconnects, its monitor fields show "—" and its chart
  curves stop; both resume when it reconnects.

## Driver test bench

To try one driver at a time without a station file of your own, use the test
bench:

```
python guitestmain.py [--station station.lab.toml]
```

- At start it asks every GPIB address for `*IDN?` (queries only). **Rescan
  GPIB** repeats the scan.
- It lists one row per driver, with the address preset to where that
  instrument answered. A driver found twice, like the two Keithley 24xx, gets
  a row per unit. You can type any other address into the box.
- **Open** shows that driver's panel in a new window and connects. Connecting
  reads only.
- Limits, rates and compliance come from the station file:
  1. the entry at the chosen address;
  2. otherwise the first entry with that driver;
  3. otherwise small built-in defaults.

  The **Settings from** column shows which one is used.
- Opening an address that is already open brings its window forward.
- Closing a window asks **Leave as is** or **Safe state, then close**.

## What connecting does (and doesn't)

**Connect** opens the session and reads the present state. It never resets an
instrument or writes to it (checked on the lab bus with writes blocked).
After connecting, **Live** polls `read()` every second (adjustable).

| Instrument | On connect (queries only) |
| --- | --- |
| Keithley 24xx | adopts the output state and source level (`sync()`), so the first ramp starts from where the unit really is. If the unit sources a different quantity than the station's `mode`, level and output are disabled until **Configure (reset)** |
| Keithley 2000 | reads the present function |
| Lake Shore 335 | read-only checks (`configure()`); if the loop controls a different input than the station reads, setpoint ramps are disabled |
| Lake Shore 625 | read-only checks against the supply's limits; snapshot (switch heater, SETI, errors) |
| LM-510 | read-only checks (model, units); snapshot (mode, interval, sensor length, low/high marks). A meter that doesn't answer is reported and disconnected |
| Giga-tronics 2420B | reads whether RF is on |
| SR830 / SR850 | snapshot: sensitivity, time constant, reference |

## Controls

Every action goes through the driver, so the station's limits, ramp rates and
power caps apply exactly as in a scan. Values accept SI prefixes: `100n`,
`7.054G`, `-1.5m`.

The Keithleys, the RF source and the lock-ins use the standard panel, built
from the driver's definition (`devices/defs/*.toml`, see
[definitions.md](definitions.md)):

- the readings, with a light for each status (in compliance);
- the mode, with **Switch mode…** where the driver has one;
- one row per setting: the present value (and its readback), an editor, and
  **Set**, or **Ramp to** and **Stop**;
- the actions.

What asks first, and what is disabled when, also comes from the definition.

| Instrument | Controls | Asks first |
| --- | --- | --- |
| Keithley 24xx | Output On (level 0, then on) / Off (ramps to 0 first); level (ramps at `max_rate`, **Stop** cancels mid-ramp); Configure (reset); **Switch mode…** between voltage and current source | Output On, Configure, Switch mode |
| Keithley 2000 | **Switch mode…** to change the function (DC V, DC I, 2-/4-wire Ω, autorange); NPLC; range (blank or `auto` for autorange) | |
| Lake Shore 335 | Ramp setpoint (K, K/min); Hold; heater range | heater range above Off |
| Lake Shore 625 | Ramp to target at a rate (shows an ETA); Ramp to zero; Hold. Never touches the switch heater | every ramp |
| LM-510 | Mode (Sample/Hold, Continuous); meter interval; Measure now | |
| Giga-tronics 2420B | Frequency; power (capped at `max_power_dbm`); RF On / Off | RF On |
| SR830 / SR850 | Sensitivity; time constant; Auto phase; reference shown read only | |

**Switch mode…** on a Keithley SMU ramps the level to 0, turns the output
OFF, and resets and configures the unit in the other function. It needs that
function's compliance, limits and max rate in the station file:

```toml
[devices.bias.modes.volt]
compliance = 1e-6
limits = [-1.0, 1.0]
max_rate = 0.1
```

The switch lasts until the GUI closes; the station file is not changed. If
the unit is found sourcing a different function than the station's, its
settings are disabled until **Configure (reset)** or **Switch mode…**.

## Cryostat instruments

The temperature controller, magnet supply and helium meter have their own
panels, laid out after the legacy LabVIEW programs in `LabviewLegacy/`. Each
has its own colour.

- **Temperature** (orange, from `TemperatureControl.vi`):
  - Big readouts for inputs A and B. The controlling input is marked, and each
    has a *stable* light (within ± tolerance for a time you set) and a rate in
    K/min (linear fit over a window you set).
  - Setpoint and heater cards, with a heater-output bar.
  - A history of A, B and the setpoint.
  - Setpoint ramp (to a setpoint at a rate, Hold).
  - Heater range buttons (Off / Low / Med / High; above Off asks first).
  - PID, shown read only. The VI's *Set PID* is deliberately left out.
- **Magnet** (blue, from `MagnetControl.vi`):
  - A big field or current readout, plus output current and voltage.
  - A bar over the station limits, showing the present value and the target.
  - Ramp progress with an ETA.
  - Lights: at target / ramping, current stable (spread over the last N
    samples), P.S. heater (read only), supply errors.
  - Ramp, Ramp to zero, Hold, and the trend.
  - The VI's persistent-switch sequence (heater on/off, auto zero, ramp
    segments) is not reproduced: the driver never writes the switch heater,
    limits or segments.
- **Helium level** (teal, from `HeliumMonitor.vi`):
  - A dewar gauge with the meter's low and high marks.
  - The level.
  - The boil-off rate in in/day, from a linear fit over 1, 6 or 24 h, as the
    VI showed.
  - The time left until the low mark.
  - Mode and meter interval, and Measure now.
  - A level chart. The panel polls every 10 s, and polling only fetches the
    last value; the meter decides when to measure, because each measurement
    boils off helium.

All three have a display-range selector on the chart (All / 30 min / 2 h /
24 h) and **Log to CSV** (one file per instrument per day, in the folder you
pick).

**Safe state** calls the drivers' `safe_state()`: sources ramp to zero then
turn off, RF turns off, the magnet and temperature hold. It works at three
levels: per instrument (on its panel), per window (window toolbar), and **All
safe state** (station window). The last two follow the station's
`shutdown_order`.

Closing the station window with instruments connected asks: **Leave as is**
(close the sessions only) or **Safe state, then close**.

## Rules

- The window owns the instrument sessions while open (single-owner rule): don't
  run a scan, or another program, on the same instruments at the same time.
- Readings on a Keithley SMU need its output on; with the output off the panel
  shows `—` instead of polling into a timeout.

## Code

`generalmeasurements/gui/`:

- `worker.py`: one asyncio loop in a background thread for all windows, so
  every session has a single owner and the windows stay responsive during a
  ramp. Callbacks are queued back to the Qt thread.
- `panels.py`: the base panel; `DefinitionPanel`, the standard look built
  from a driver's definition (Keithleys, RF source, lock-ins); and
  `GenericPanel` for simulators and any Movable/Rampable without a definition.
- `instruments/`: the panels with their own look, one module per instrument,
  named like its driver (`instruments/lakeshore_625.py` ↔
  `devices/lakeshore_625.py`).
- `widgets.py`: LEDs, cards, the dewar gauge, the field bar, the wall-clock
  chart, CSV logging, and the stable/rate fits.
- `registry.py`: driver name → panel class.
- `hub.py`: `DataHub`. Each panel emits `Panel.reading(name, time, data)` from
  `_got()`, the one place poll data leaves a panel. The hub re-broadcasts it
  per field key (`updated`), tracks connections (`device_changed`) and keeps
  the latest value per field.
- `views.py`: the field picker, `MonitorView`, `ChartView`, and `ViewWindow`
  (one QtAds dock, saved with the layout).
- `app.py`: `StationWindow` owns the worker, the sessions and the panels;
  `InstrumentWindow` holds one `CDockManager`; `WINDOW_OF_DRIVER` maps drivers
  to windows; layout save and restore.

Tests are in `tests/test_gui.py` and `tests/test_cryo.py`: offscreen, on
simulators and fake transports.

Drivers stay free of Qt, so scans, `gm validate` and CI don't need it, and the
safety checks live in one place that the GUI can only call into.

To add a new instrument:

1. Write the driver and its definition, `devices/defs/<driver>.toml`
   ([definitions.md](definitions.md)).
2. For the standard look, map the driver to `DefinitionPanel` in `PANELS`
   (`panels.py`); no GUI code is needed. For a custom look, subclass `Panel`
   in `instruments/<driver>.py` and fill in `build_layout()`, `attach()` and
   `show_data()`.
3. Map the driver to a window in `WINDOW_OF_DRIVER`.
