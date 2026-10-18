# Keithley 2400 / 2401 / 2410 / 2450 (`keithley_24xx`)

Source-measure unit. One class, `Keithley24xx`, covers the four models through
a small per-model dialect table. Contracts: Movable, Readable, Configurable
(+ `prepare()`).

**Verification status: 2400 and 2401 communication verified on hardware, with
the output OFF** (MODEL 2400 fw C24, MODEL 2401 fw B02, GPIB, 2026-10-16):
`configure()`, stepped `set()`, the compliance query, `snapshot()` and
`safe_state()` are accepted with an empty error queue and the settings read
back as written (`tests/test_live_keithley.py`, transcripts in
`tests/golden/live_2400.txt`, `live_2401.txt`). Not yet verified: `read()`
with the output ON (needs a dummy load), and anything on a 2410 or 2450.

## Station config

```toml
[devices.gate]
driver     = "keithley_24xx"
model      = "2400"          # 2400 | 2401 | 2410 | 2450
resource   = "GPIB0::24::INSTR"
mode       = "volt"          # sourced quantity: volt | curr
compliance = 1e-9            # A in volt mode, V in curr mode
limits     = [-10.0, 10.0]   # checked at plan build and in set()
max_rate   = 0.5             # V/s (A/s), enforced inside set()
nplc       = 1.0
```

`limits` must fit the model's range (2400: ±210 V, 2401: ±20 V, 2410: ±1100 V,
2450: ±200 V; ±1.05 A), checked at construction.

## Commands used

| Step | Commands (2400 family) | 2450 differences |
| --- | --- | --- |
| `configure()` | `*RST`; `:SOUR:FUNC VOLT\|CURR`; `:SOUR:<f>:MODE FIX`; `:SOUR:<f>:LEV 0`; `:SENS:CURR:PROT <c>` (volt mode) or `:SENS:VOLT:PROT <c>`; `:SENS:FUNC "VOLT","CURR"`; `:SENS:<other>:NPLC <n>`; `:FORM:ELEM VOLT,CURR` | compliance `:SOUR:VOLT:ILIM` / `:SOUR:CURR:VLIM`; `:SENS:FUNC "<other>"` (CURR in volt mode, VOLT in curr mode); no `:FORM:ELEM` |
| `prepare()` (runner, before first move) | `:SOUR:<f>:LEV 0`; `:OUTP ON` | same |
| `set(x)` | repeated `:SOUR:<f>:LEV <v>` in steps of `max_rate*step_dt`, `step_dt` apart, then `settle_s` | same |
| `read()` | `:READ?` (reply `V,I,...`) then the compliance-trip query | `:READ? "defbuffer1", SOUR, READ` (reply `source,sensed`; mapped to V/I by mode); trip query `:SOUR:VOLT:ILIM:TRIP?` / `:SOUR:CURR:VLIM:TRIP?` |
| compliance query | `:SENS:CURR:PROT:TRIP?` / `:SENS:VOLT:PROT:TRIP?` | see above |
| `snapshot()` | `*IDN?` + driver settings | same |
| `safe_state()` | ramp to 0 at `max_rate` (if 0 is inside limits), then `:OUTP OFF` | same |

`read()` returns `<name>.v`, `<name>.i` and `<name>.compliance` (0/1). Use
`compliance_watchdog("<name>.compliance")` to abort on a trip.

## `:READ?` vs `:FETCh?`

The driver uses `:READ?` (= `:ABORt; :INITiate; :FETCh?`): it triggers a fresh
measurement and returns it. `:FETCh?` returns the last stored reading without
triggering, so after a move-and-settle it would return stale data. Never use
`:MEASure?`: it resets the configuration. Revisit only for hardware-timed
buffered sweeps (a non-goal for v1).

## Quirks and known gaps

- **2450 reading.** A bare `:READ?` returns only the sensed value, so the
  driver asks for the buffer elements explicitly
  (`:READ? "defbuffer1", SOUR, READ`) and maps `source,sensed` to V/I by mode.
  `SOUR` is the measured source value only while source readback is on (the
  2450 default; `*RST` keeps it on). Still needs a golden transcript.
- **`read()` needs the output on.** With the output off, `:READ?` logs
  `803,"Not permitted with OUTPUT off"` and never replies (a 4 s VISA
  timeout, then `-420,"Query UNTERMINATED"`). `read()` now raises
  `DriverError` at once when the driver believes the output is off; the runner
  calls `prepare()` (output ON) before the first read.
- `configure()` sends `*CLS` after `*RST` and finishes with `:SYST:ERR?` until
  empty; any rejected command raises `DriverError`. `errors()` drains the
  queue on demand.
- A plain query round trip is about 6 ms on GPIB.
- The driver keeps its own belief of the source level (`_level`, 0 after
  `configure()`); it never reads the level back. After a crash or a manual
  front-panel change the belief can be wrong; re-run `configure()` (the CLI
  does at startup, which does `*RST` and leaves the output OFF).
- Compliance is reported per reading; the instrument itself clamps. A trip
  between two reads is only seen if the flag is still set at the next read.
- No `*OPC?`, and the error queue is only checked in `configure()`: a command
  rejected during a run is not noticed until the next `configure()`.
- Only the opposite quantity's NPLC is set (current NPLC in volt mode).
- `safe_state()` with 0 outside the limits holds the current level and turns
  the output off (does not step through an out-of-range zero).
- 4-wire sense, autorange, digital I/O, filtering: not exposed.

## Source function (mode)

The definition is split per function: `devices/defs/keithley_24xx.toml` (output,
readings, NPLC, Configure) plus `keithley_24xx_volt.toml` /
`keithley_24xx_curr.toml` (the level and compliance in their units, and which
reading confirms the level). `switch_mode(mode, compliance, limits, max_rate)`:

1. checks the new settings against the model's range, before any I/O;
2. ramps to 0 at the old max rate and turns the output OFF;
3. runs `configure()` (`*RST`) in the new function.

The GUI takes the settings from `[devices.X.modes.<mode>]` in the station.
Not yet run on hardware.

## Tests

`tests/test_devices.py`, `tests/test_definitions.py` (mode switch order),
`tests/test_safety.py` (ramp step size, compliance
abort ramps to zero, prepare/safe-state ordering). Hardware:
`tests/test_live_keithley.py` (`SWEEP_LIVE=1`, outputs stay off; see the file
header for the resource variables).
