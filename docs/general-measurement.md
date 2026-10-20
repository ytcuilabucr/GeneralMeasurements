# General Measurement

Builds and runs measurements on the Instrument Server's connected instruments.
It follows the workflow of Labber's **Measurement Editor**. Open it from the
server's **Window → General Measurement**.

This is the skeleton: the Labber workflow from picking channels to a data file
works end to end. What is left out is listed at the end.

## Labber's Measurement Editor: how it works

Labber splits the lab into two programs. The **Instrument Server** owns the
instrument connections. The **Measurement Editor** builds measurements out of
the instruments' *quantities* (settings and readings). Its logic:

1. **Channels, not instruments.** A measurement is made of channels: one
   instrument quantity each ("Keithley 1 – Source voltage", "Lock-in – X").
   The editor lists the server's instruments and their quantities, and you add
   quantities to two lists.
2. **Step sequence.** These are the quantities to step. Each row is a channel
   with its values. **The first row is the innermost loop**, stepped fastest; each
   row below is the next loop out. A channel with a single value is no loop:
   it is set once, before the measurement starts. This is how Labber holds
   constant settings, such as a fixed gate voltage, for one measurement.
3. **Step items.** A channel's values are built from one or more **step items**.
   Their values are joined in order. Each item is:
   - a range type: *Single* value, *Start – stop*, or *Center – span*;
   - a step type: *Number of points* or *Step size* (the other one is worked
     out);
   - an interpolation: linear or logarithmic.

   Several items make non-uniform sweeps: coarse, then fine near a feature,
   then coarse.
4. **Log channels.** These are the readings recorded at every point of the
   step sequence.
5. **Options per measurement:** a wait time after each step; what to do at the
   end; a comment; the data file's name.
6. **Start.** Before starting, the editor shows the number of points and an
   estimated time. The run writes one data file, filed by date in the
   database folder. A viewer plots the data live, and the run can be paused
   or stopped.
7. The data file keeps the measurement's definition and the instrument
   settings. A measurement can therefore be reopened from its data and run
   again.

Labber has more on top of this: channel relations (a channel computed from
others by a formula), sweep modes (ramping between points or continuously),
hardware triggering, the optimizer, and an instrument-settings snapshot that
Labber **writes back** to the instruments at the start.

## The same workflow here

| Labber | General Measurement | Where |
| --- | --- | --- |
| Instrument Server | Instrument Server (same program, single owner) | `gui/server/` |
| Instrument quantities | **Set** and **Read** channels of each connected instrument | `measurement/channels.py` |
| Step sequence, first = inner loop | **Step sequence**, first = inner loop; single values set once, first | `measurement/config.py` |
| Step items (single, start – stop, center – span; points or step; linear or log) | the same | `StepItem` |
| Log channels | **Log channels** | `MeasurementConfig.log` |
| Wait time | **Wait after set** per channel; **Alternate direction** (snake) | `StepChannel` |
| Estimated time | points and time next to Start, using each logged instrument's measured read time (the panels' `read … ms`) | `window.py` |
| Database folder, date-filed | `<data folder>/YYYY/MM/Data_MMDD/<name>.h5`, never overwritten (`_2`, `_3`, ...) | `data_path()` |
| Log Viewer | live plot of a log channel along the inner loop | `window.py` |
| Measurement saved with the data | the measurement's TOML is stored in the file's provenance; also **File → Save** | `MeasurementConfig.to_toml()` |

The channels come from the drivers' definitions (`devices/defs/*.toml`), so a
new instrument needs no measurement code:

- **Set** channels are numeric properties that are writable and have a `set`
  method, such as a Keithley's level, a lock-in's frequency, amplitude and
  phase, or a Lake Shore 335 setpoint. Their limits are the definition's
  (`limits`, `min`, `max`).
- An instrument with a plain `set()` and `limits` that no property covers also
  gets a Set channel: the simulators (`<name>.value`) and the Lake Shore 625's
  field target.
- **Read** channels are the fields of `describe()`, the same readings the panel
  and the Quick view show.

## Running

**Check** checks every point against the channels' limits and estimates the
time, without setting anything. **Start** asks first: it lists every channel it
will set, the after-run action and the data file. Then it runs the ordinary
runner (`engine/runner.py`) on the server's sessions, in the server's worker
loop:

- **Before the first point nothing is done:** no reset, and no output is turned
  on. A Keithley whose output is off is not read; its readings are NaN.
- **At each point** only the channels whose value changed are set. Then
  "Wait after set" passes, and the logged instruments are read together.
- **Pause** holds the instruments at their values; **Stop** aborts at once, even
  in the middle of a ramp.
- **After the run**, it either leaves everything at the last values (Labber's
  behaviour), or calls `safe_state()` on every instrument used, in the
  config's `shutdown_order` first.
- **The data file** is the standard HDF5 layout (`sinks/hdf5.py`): the
  setpoints, one dataset per log channel with the grid's shape, the
  instruments' snapshots, and the measurement itself. While a measurement
  runs, its editor is locked, and neither window can be closed.

Panels keep polling during a run; each instrument's session lock keeps
commands in order.

## Time traces: the Timer

Labber does this with its virtual **Timer** instrument
([Timer.ini](https://github.com/Labber-software/Drivers/blob/master/Timer/Timer.ini),
[Timer.py](https://github.com/Labber-software/Drivers/blob/master/Timer/Timer.py)).
It has two quantities:
- **Timer:** setting *t* waits until *t* seconds have passed since the timer
  was reset, and setting 0 resets it.
- **Clock:** reads the time since the reset.

Stepped as the inner loop, from 0 to the trace length, it gives one point per
step at fixed times. The parameter that changes in the outer loop is set once
per pass.

General Measurement has the same Timer built in (`gmcore/measurement/timer.py`).
It is listed under the instruments as **Timer**, with the step channel
`Timer.timer` and the log channel `Timer.clock`.

- **Waits are to absolute times.** Each point waits until `reset + t`, not for
  one step after the last point. A read therefore never pushes the later points
  back, and the grid 0, dt, 2·dt, … doesn't drift.
- **Waits are precise.** Most of a wait is a sleep. The last 20 ms is polled,
  because the event loop's timer is only good to about 15 ms on Windows. On the
  lab PC, a 20 ms step lands within 0.5 ms of the grid, with 0.1 ms jitter.
- **The timer resets** at its first set, at 0, and at a value lower than the
  last one: the next pass of an outer loop.
- **It starts after the outer value is in place.** The Timer is set after the
  other changed channels and their "Wait after set" (axis `stage` 1). Each pass
  therefore starts its clock once the outer value is set.
- **Log `Timer.clock`** to record the real time of every point. It is read
  together with the instruments.
- **A point that is already due is taken at once.** This happens when the reads
  take longer than the step. The estimate next to Start warns about it, in
  amber, when the slowest direct read is not shorter than the step.
- **During a pause the clock keeps running.** The points that fall due during
  the pause are taken one after another after Resume.

A typical trace: **Timer** from 0 to 60 s, 601 points, as the first (inner)
step channel. Then the parameter, e.g. a gate voltage, as the second step
channel with the values to visit. Log `Timer.clock` and the readings.

## Readings from: direct or the server's register

Each logged instrument is read in one of two ways (**Readings from**, in the
Measurement box):

- **Direct:** the measurement reads the instrument at every point. This is
  Labber's behaviour, and the default when nothing is timed.
- **Server register:** the measurement never talks to the instrument. Its panel
  in the Instrument Server reads the hardware at the panel's own interval (the
  Live setting), and every poll goes into the server's **register**
  (`gmcore/measurement/register.py`), which keeps the latest value of every
  field. The measurement takes the register's latest values at its own times.
  - **Hardware traffic:** the hardware is read by the server only, at one rate,
    however often the measurement samples.
  - **Sample age:** a sampled instrument also records
    `<instrument>.sample_age` (s), how old the oldest sampled value was. Stale
    data is therefore visible in the file. A disconnected instrument samples
    NaN.

**Auto** (the default) decides for each logged instrument:
- **Server register:** when the Timer is stepped and the instrument's panel
  polls at another interval than the Timer's step (more than 1 % apart).
- **Direct:** when the intervals match, when the panel isn't polling (Live off:
  nothing else reads it), or when there is no Timer.

The line next to the setting shows each instrument's choice, e.g.
"lockin: server register (panel reads every 0.2 s)". The Start confirmation
lists it too, and the data file's plan metadata records it (`log_sources`,
`server_intervals_s`).

**Server register** forces sampling. It refuses to start an instrument whose
panel isn't polling. **Direct** forces direct reads.

## Measurement files

**File → Save** writes the measurement as TOML (default folder
`Measurement config/`). A file can be edited by hand:

```toml
name = "gate sweep"
comment = ""
after = "hold"                      # or "safe"
data_dir = "Data"
log_source = "auto"                 # auto / direct / register
log = ["lockin.x", "lockin.y"]

[[step]]                            # the first is the inner loop
channel = "gate.value"
settle_s = 0.0
snake = false

[[step.items]]
range_type = "start_stop"           # single / start_stop / center_span
start = -1.0
stop = 1.0
step_type = "points"                # points / step
n_points = 41
step = 0.1
interpolation = "linear"            # linear / log
```

A channel whose instrument isn't connected is marked in amber, and the
measurement can't start until it is.

## Not in the skeleton yet

- **Stepping enum and bool settings** (ranges, functions, output on/off).
  Labber can step these; here only numbers are steppable.
- **Channel relations**, Labber's formulas such as `gate2 = -gate1`.
- **Sweep modes:** continuous ramps while reading. The engine has fly scans
  (`plans/fly.py`), but the editor doesn't offer them yet.
- **A 2D image of the last two loops.** `PlotBuffer.image()` exists; the
  window shows only the line being measured.
- **Drag and drop** from the instrument tree. Use double-click, **Step it** or
  **Log it**.
- **Writing a stored instrument snapshot back before the run.** Labber does
  this, and it is deliberately not done here: nothing is written to an
  instrument except the step channels' values.
- **Scaling channels:** gain, offset, and the user's own names and units.

## Code

- `gmcore/measurement/`, none of which uses Qt:
  - `config.py`: the measurement, as TOML.
  - `channels.py`: the channel catalog; the adapters `StepTarget`, `LogTarget`
    and `InstrumentRef`; `log_sources()`; `estimate_s()`; and `build()`, which
    returns the Scan and the runner's devices.
  - `timer.py`: the Timer.
  - `register.py`: the server's `Register` and `RegisterTarget`.
- `gmcore/gui/measurement/window.py`, with the form
  `gui/forms/general_measurement.ui`.
- Tests:
  - `tests/test_measurement.py`: on the simulators, end to end through the
    window, including a time trace that samples the register.
  - `tests/test_timer.py`: the Timer's waits and resets, the grid through the
    runner with slow reads, the set order, the estimate, and the log-source
    decision.
