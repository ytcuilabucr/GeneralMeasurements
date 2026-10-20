# Keithley 2400 / 2401 / 2410 / 2450 SourceMeters

One independent driver per model, each with its own definition files. They
share no code ([the rule](../adding-an-adapter.md#the-rule-one-instrument-model-one-driver)):

| Driver | Class | Module | Definition |
| --- | --- | --- | --- |
| `keithley_2400` | `Keithley2400` | `gmcore/devices/keithley_2400.py` | `defs/keithley_2400.toml`, `_volt`, `_curr` |
| `keithley_2401` | `Keithley2401` | `gmcore/devices/keithley_2401.py` | `defs/keithley_2401.toml`, `_volt`, `_curr` |
| `keithley_2410` | `Keithley2410` | `gmcore/devices/keithley_2410.py` | `defs/keithley_2410.toml`, `_volt`, `_curr` |
| `keithley_2450` | `Keithley2450` | `gmcore/devices/keithley_2450.py` | `defs/keithley_2450.toml`, `_volt`, `_curr` |

The 2400, 2401 and 2410 use the 2400 command set and their code is the same
apart from each model's figures (class attributes `V_RANGES`, `I_RANGES`,
`MAX_V`, `HIGH_V_MAX_I`, ...). The 2450 file has the 2450's commands. A fix to
the shared behaviour goes into each file. Contracts: Movable, Readable,
Configurable (+ `prepare()`).

**Verification status: 2400 and 2401 communication verified on hardware, with
the output OFF** (MODEL 2400 fw C24, MODEL 2401 fw B02, GPIB, 2026-10-16):
`configure()`, stepped `set()`, the compliance query, `snapshot()` and
`safe_state()` are accepted with an empty error queue and the settings read
back as written (`tests/test_live_keithley.py`, transcripts in
`tests/golden/live_2400.txt`, `live_2401.txt`). Not yet verified on hardware:
`read()` with the output ON (needs a dummy load), the range commands, and
anything on a 2410 or 2450.

## Model figures (from the manuals)

Sources:
- Series 2400 datasheet (1KW-2798-1);
- Model 2401 datasheet;
- Series 2400 User's Manual (2400S-900-01 Rev. K);
- Model 2450 specifications (SPEC-2450 Rev. B);
- Model 2450 Reference Manual (2450-901-01 Rev. D).

| | 2400 | 2401 | 2410 | 2450 |
| --- | --- | --- | --- | --- |
| Voltage ranges | 200 mV, 2, 20, 200 V | 200 mV, 2, 20 V | 200 mV, 2, 20, 1000 V | 20 mV, 200 mV, 2, 20, 200 V |
| Current ranges | 1 µA … 1 A (decades) | 1 µA … 1 A | 1 µA, 10 µA, 100 µA, 1 mA, **20 mA**, 100 mA, 1 A | **10 nA**, 100 nA, 1 µA … 1 A |
| Source limits | ±21 V @ ±1.05 A; ±210 V @ ±105 mA | ±21 V @ ±1.05 A | ±21 V @ ±1.05 A; ±1100 V @ ±21 mA | ±21 V @ ±1.05 A; ±210 V @ ±105 mA |
| Compliance commands | `:SENS:CURR:PROT`, `:SENS:VOLT:PROT` | same | same | `:SOUR:VOLT:ILIM`, `:SOUR:CURR:VLIM` |
| Minimum compliance | 0.1 % of range | 0.1 % | 0.1 % | more than 0.1 % of the measure range (reference manual; the spec sheet says 10 %) |

All four: overrange 105 % (source and measure); NPLC 0.01 … 10; after `*RST`
both source and measure autorange are ON. The 2450's 10 nA and 100 nA ranges
are on the rear triax connectors only.

The driver checks these when it is built, before any I/O (and `gm validate`
builds it without opening the session):
- the limits against the model's maximum;
- the compliance against the model's maximum and the 0.1 % minimum;
- the power envelope: above ±21 V the compliance may not exceed 105 mA on
  the 2400 and 2450, or 21 mA on the 2410. Sourcing current above that, the
  voltage compliance may not exceed 21 V;
- NPLC, and any station range against the model's list.

## Station config

```toml
[devices."Keithley 2400 - 1"]
driver     = "keithley_2400"     # keithley_2400 | keithley_2401 | keithley_2410 | keithley_2450
resource   = "GPIB0::24::INSTR"
mode       = "volt"              # sourced quantity: volt | curr
compliance = 1e-9                # A in volt mode, V in curr mode
limits     = [-10.0, 10.0]       # checked at plan build and in set()
nplc       = 1.0                 # within min / max of `nplc` in the model's .toml (0.01 … 10)
source_range = 20.0              # optional; a fixed source range (omit: autorange)
sense_range  = 1e-6              # optional; a fixed measure range for the compliance quantity
```

## Ranges

| Control (GUI level 1) | Command | Notes |
| --- | --- | --- |
| Source range | `:SOUR:<f>:RANG <n>` / `:SOUR:<f>:RANG:AUTO ON` | Finer resolution on a fixed range. A level beyond 105 % of the range is refused by the unit, so `set()` refuses it first. Changed **only with the output off**: a range change glitches the output (range-change overshoot: 100 mV typical on the 2400 family, 250 mV on the 2450). The driver also refuses a range the limits don't fit in |
| Measure range | `:SENS:<other>:RANG <n>` / `…:RANG:AUTO ON` | The range of the compliance quantity (the measured one). Below the compliance setting it limits the output too |
| Effective compliance | (computed) | The compliance, or 105 % of a lower fixed measure range |

The measure range and the compliance interact:
- **2400 family: "range compliance".** With compliance 1 mA and a fixed 10 µA
  measure range, the output clamps at 10.5 µA. Autorange never does this.
  This is the safe direction: a lower range only lowers the clamp.
- **2450: the limit itself moves.** A measure-range change can lower the
  current/voltage limit (an event), so after a change the driver re-reads the
  limit into `compliance`.

The sourced quantity is measured on the source range; the manuals don't allow
a separate measure range for it.

Range writes are followed by `:SYST:ERR?`; a rejected setting raises
`DriverError` and the panel shows the unit's message.

## Commands used

| Step | Commands (2400 family) | 2450 differences |
| --- | --- | --- |
| `configure()` | `*RST`; `:SOUR:FUNC VOLT\|CURR`; `:SOUR:<f>:MODE FIX`; `:SOUR:<f>:LEV 0`; `:SENS:CURR:PROT <c>` (volt mode) or `:SENS:VOLT:PROT <c>`; `:SENS:FUNC "VOLT","CURR"`; `:SENS:<other>:NPLC <n>`; `:FORM:ELEM VOLT,CURR`; then the station's ranges if given | compliance `:SOUR:VOLT:ILIM` / `:SOUR:CURR:VLIM`; `:SENS:FUNC "<other>"`; no `:FORM:ELEM` |
| `prepare()` (runner, before first move) | `:SOUR:<f>:LEV 0`; `:OUTP ON` | same |
| `set(x)` | one `:SOUR:<f>:LEV <x>` (no ramp), then `settle_s` | same |
| `set_nplc(n)` | `:SENS:<other>:NPLC <n>`, then `:SYST:ERR?`; `n` within the definition's min / max | same |
| `read()` | `:READ?` (reply `V,I,...`) then the compliance-trip query | `:READ? "defbuffer1", SOUR, READ` (reply `source,sensed`; mapped to V/I by mode); trip query `:SOUR:VOLT:ILIM:TRIP?` / `:SOUR:CURR:VLIM:TRIP?` |
| ranges | `:SOUR:<f>:RANG[:AUTO]`, `:SENS:<f>:RANG[:AUTO]` and their queries | same; then `:SOUR:<f>:<x>LIM?` |
| `snapshot()` | `*IDN?` + driver settings and ranges | same |
| `safe_state()` | `:SOUR:<f>:LEV 0` in one step (if 0 is inside limits), then `:OUTP OFF` | same |

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
- No `*OPC?`; the error queue is checked in `configure()` and after range
  writes, not after level writes.
- Only the opposite quantity's NPLC is set (current NPLC in volt mode).
- `safe_state()` with 0 outside the limits holds the current level and turns
  the output off (does not step through an out-of-range zero).
- 4-wire sense, digital I/O, filtering, the 2410's and 2450's interlocks:
  not exposed.

## Source function (mode)

Each model's definition is split per function: `defs/keithley_<model>.toml`
(output, readings, NPLC, Configure) plus `keithley_<model>_volt.toml` /
`keithley_<model>_curr.toml`. The mode files hold the level, the compliance and
the ranges in their units, and say which reading confirms the level.
`switch_mode(mode, compliance, limits)`:

1. refuses unless the output is OFF (the level may be anything);
2. checks the new settings against the model, before any I/O;
3. runs `configure()` (`*RST`) in the new function, with both ranges on auto,
   so it starts at level 0 with the output off.

The GUI enables **Switch mode…** only with the output OFF
(`switch_interlock = "!output"`). It asks for the settings, pre-filled from
`[devices.X.modes.<mode>]` in the station or else from the mode file's
`switch_defaults`.
Not yet run on hardware.

## Tests

- `tests/test_devices.py`: model ranges, model limits, ranges and effective compliance;
- `tests/test_definitions.py`: the mode switch order;
- `tests/test_safety.py`: one-step set, compliance abort sets zero, prepare/safe-state ordering;
- `tests/test_gui.py`: the range drop-downs.

Hardware: `tests/test_live_keithley.py` (`SWEEP_LIVE=1`, outputs stay off; see
the file header for the resource variables).
