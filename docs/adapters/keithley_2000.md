# Keithley 2000 DMM (`keithley_2000`)

6.5-digit multimeter. Contracts: Readable, Configurable. Input only: it has no
`prepare()` and its `safe_state()` does nothing.

**Verification status: not run against hardware** (fake-transport tests only).

## Station config

```toml
[devices.dmm]
driver   = "keithley_2000"
resource = "GPIB0::16::INSTR"
function = "volt"     # volt | curr | res | fres (DC functions)
nplc     = 10
range    = 1.0        # omit for autorange
```

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `:SENS:FUNC '<f>'` (`VOLT:DC`, `CURR:DC`, `RES`, `FRES`); `:SENS:<f>:NPLC <n>`; `:SENS:<f>:RANG:AUTO ON` or `:SENS:<f>:RANG <r>` |
| `read()` | `:READ?`, first comma-separated field, unit suffix stripped |
| `snapshot()` | `*IDN?`, function, nplc, range |

`read()` returns `{<name>: value}` in V, A or Ohm.

## Quirks and known gaps

- Autorange makes read time vary between points; fix the range for timing-
  sensitive fly scans.
- Suffix stripping is a guess about the reply format; confirm with a
  transcript (the reply may be plain `+1.234500E-03` depending on `:FORM`).
- AC functions, frequency, temperature, math and scanner cards: not exposed.
- No error-queue polling.

## Tests

`tests/test_devices.py::test_k2000_read`.
