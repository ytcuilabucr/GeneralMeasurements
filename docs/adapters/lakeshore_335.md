# Lake Shore 335 temperature controller (`lakeshore_335`)

One control loop as a Rampable in kelvin. Contracts: Rampable (so Movable and
Readable), Configurable (+ `prepare()`).

**Verification status: queries only.** Reply formats of `*IDN?`, `KRDG?`,
`SETP?`, `RANGE?`, `RAMP?`, `RAMPST?`, `HTR?` and `OUTMODE?` were read from a
real 335 (fw 1.4, GPIB, 2026-10-16) and are used in the fake-transport tests.
No write (`SETP`, `RAMP`, `RANGE`) has been sent to the instrument yet.

## Station config

```toml
[devices.temp]
driver        = "lakeshore_335"
resource      = "GPIB0::1::INSTR"
input         = "A"          # sensor read as the axis value: A | B
loop          = 1            # heater output: 1 | 2
limits        = [2.0, 300.0] # K, checked before any move
max_rate      = 0.0333       # K/s (= 2 K/min); the 335 takes 0.1-100 K/min
heater_range  = 2            # set by prepare(): 0 off, 1 low, 2 med, 3 high; omit to leave as is
tolerance     = 0.1          # K, for set() "settled"
stable_s      = 60           # s inside tolerance before set() returns
settle_timeout_s = 3600
safe_action   = "hold"       # hold: freeze the setpoint ramp | off: heater range 0
```

Line termination is CR LF (set by the registry).

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `*IDN?` (must be MODEL335); `OUTMODE? <loop>`. **No writes.** Raises if the loop's output mode is Off, or if the loop is controlled by a different input than `input` |
| `prepare()` | `RANGE <loop>,<heater_range>` (only if `heater_range` is set) |
| `start_ramp(T, rate)` | `RAMP <loop>,1,<rate K/min>`; `SETP <loop>,<T>` |
| `ramp_done()` | `RAMPST? <loop>` = 0 |
| `set(T)` | `start_ramp(T, max_rate)`, then polls `RAMPST?` and the temperature until within `tolerance` for `stable_s` |
| `read()` | `RDGST? <input>`, `KRDG? <input>`, `SETP? <loop>`, `HTR? <loop>` |
| `snapshot()` | `*IDN?`, `SETP?`, `RAMP?`, `RANGE?`, `OUTMODE?`, `PID?` |
| `safe_state()` | hold: `SETP?` then `RAMP <loop>,0,<rate>`; `SETP <loop>,<current setpoint>`. off: `RANGE <loop>,0` |

`read()` returns `<name>` (K), `<name>.setpoint` (K) and `<name>.heater` (%).
A non-zero `RDGST?` (sensor fault, over/underrange) makes `<name>` NaN, so
`nan_watchdog` catches it.

## Quirks and known gaps

- `ramp_done()` means the *setpoint* ramp finished. The temperature lags
  behind it, so a fly scan records the readback (`<name>`), not the setpoint.
- With the setpoint ramp on, a new `SETP` ramps from the present setpoint, not
  from the present temperature.
- The driver never changes the control input, PID, heater resistance or zone
  settings; set those on the front panel. On 2026-10-16, loop 1 was set to
  control input B, which read 0 K, while input A read 77.6 K; `configure()`
  refuses that combination unless `input = "B"`.
- Rate resolution is 0.1 K/min; requested rates are rounded.

## Tests

`tests/test_gpib_adapters.py` (`test_ls335_*`).
