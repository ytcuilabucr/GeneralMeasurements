# Keithley 2400 / 2401 / 2410 / 2450 (`keithley_24xx`)

Source-measure unit. One class, `Keithley24xx`, covers the four models through
a small per-model dialect table. Contracts: Movable, Readable, Configurable
(+ `prepare()`).

**Verification status: not run against hardware.** Command strings are from
memory of the manuals and covered only by fake-transport tests. Treat every
string below as "needs a golden transcript from the real unit". The 2450 is
the least certain (see Quirks).

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
| `configure()` | `*RST`; `:SOUR:FUNC VOLT\|CURR`; `:SOUR:<f>:MODE FIX`; `:SOUR:<f>:LEV 0`; `:SENS:CURR:PROT <c>` (volt mode) or `:SENS:VOLT:PROT <c>`; `:SENS:FUNC "VOLT","CURR"`; `:SENS:<other>:NPLC <n>`; `:FORM:ELEM VOLT,CURR` | compliance `:SOUR:VOLT:ILIM` / `:SOUR:CURR:VLIM`; `:SENS:FUNC "CURR"`; no `:FORM:ELEM` |
| `prepare()` (runner, before first move) | `:SOUR:<f>:LEV 0`; `:OUTP ON` | same |
| `set(x)` | repeated `:SOUR:<f>:LEV <v>` in steps of `max_rate*step_dt`, `step_dt` apart, then `settle_s` | same |
| `read()` | `:READ?` then the compliance-trip query | trip query `:SOUR:VOLT:ILIM:TRIP?` |
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

- **2450 reading likely wrong.** Its `:READ?` probably returns only the sensed
  function, but `read()` expects `V,I` and raises `bad reading`. Fix is an
  explicit buffer read (`:READ? "defbuffer1", SOUR, READ`) and a golden
  transcript. Not done.
- The driver keeps its own belief of the source level (`_level`, 0 after
  `configure()`); it never reads the level back. After a crash or a manual
  front-panel change the belief can be wrong; re-run `configure()` (the CLI
  does at startup, which does `*RST` and leaves the output OFF).
- Compliance is reported per reading; the instrument itself clamps. A trip
  between two reads is only seen if the flag is still set at the next read.
- No `:SYST:ERR?` polling and no `*OPC?`: a rejected command is not noticed.
- Only the opposite quantity's NPLC is set (current NPLC in volt mode).
- `safe_state()` with 0 outside the limits holds the current level and turns
  the output off (does not step through an out-of-range zero).
- 4-wire sense, autorange, digital I/O, filtering: not exposed.

## Tests

`tests/test_devices.py`, `tests/test_safety.py` (ramp step size, compliance
abort ramps to zero, prepare/safe-state ordering). Hardware tests (`SWEEP_LIVE=1`,
dummy load only) are not written yet.
