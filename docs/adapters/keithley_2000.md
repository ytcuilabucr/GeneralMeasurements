# Keithley 2000 DMM (`keithley_2000`)

6.5-digit multimeter. Contracts: Readable, Configurable. Input only: it has no
`prepare()` and its `safe_state()` does nothing.

**Verification status: communication verified on hardware** (MODEL 2000,
fw A20/A02, GPIB, 2026-10-16): every command below is accepted with an empty
error queue; `tests/test_live_keithley.py`, transcript in
`tests/golden/live_2000.txt`. Measurement accuracy not yet checked against a
reference.

## Station config

```toml
[devices.dmm]
driver   = "keithley_2000"
resource = "GPIB0::16::INSTR"
function = "volt"     # volt | curr | res | fres (DC functions)
nplc     = 10
range    = 1.0        # one of the function's ranges (below); omit for autorange
```

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `*CLS`; `:INIT:CONT OFF`; `:SENS:FUNC '<f>'` (`VOLT:DC`, `CURR:DC`, `RES`, `FRES`); `:SENS:<f>:NPLC <n>`; `:SENS:<f>:RANG:AUTO ON` or `:SENS:<f>:RANG <r>`; then `:SYST:ERR?` until empty (raises if anything was rejected) |
| `read()` | `:READ?`; reply is plain `+5.88670672E+00` |
| `errors()` | `:SYST:ERR?` until `0,"No error"` |
| `snapshot()` | `*IDN?`, function, nplc, range |

`read()` returns `{<name>: value}` in V, A or Ohm. An open input or overrange
reads `9.9e+37`; use `range_watchdog` to catch it.

## Timing (measured)

`:READ?` round trip: 41 ms at NPLC 0.1, 37 ms at NPLC 1, 531 ms at NPLC 10
(60 Hz mains). A plain query takes about 6 ms.

## Quirks and known gaps

- **Continuous triggering.** Front-panel use leaves `:INIT:CONT ON`; then every
  `:READ?` still returns a value but logs `-213,"Init ignored"`. `configure()`
  turns it off, so the front-panel display stops updating between reads.
- **Late replies after a timeout.** Before the transport fix, a `:READ?` that
  timed out left its reply in the instrument, and the next query returned
  `'-KEITHLEY INSTRUMENTS...'` (a stray byte of the late reading). The
  transport now device-clears on every timeout; covered by
  `test_k2000_timeout_then_next_query_in_sync`.
- Autorange makes read time vary between points; fix the range for timing-
  sensitive fly scans.
- AC functions, frequency, temperature, math and scanner cards: not exposed.

## Function (mode)

One definition file per function (`devices/defs/keithley_2000_<function>.toml`,
which set the reading's and the range's unit). Ranges, from the User's Manual
(2000-900-01 Rev. J): DC volts 100 mV, 1, 10, 100, 1000 V; DC current 10 mA,
100 mA, 1 A, 3 A; ohms 100 Ω … 100 MΩ in decades. The GUI's range is a
drop-down of the present function's ranges plus Auto; NPLC 0.01 … 10.
`switch_mode(function)`
configures the new function with autorange; `set_nplc()` and `set_range()`
change one setting and check the error queue. On connect the driver adopts
the meter's present function. Not yet run on hardware.

## Tests

`tests/test_devices.py::test_k2000_read`, `tests/test_definitions.py`; live: `tests/test_live_keithley.py`
(`test_k2000_*`).
