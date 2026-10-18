# Giga-tronics 2420B signal generator (`gigatronics_2420b`)

Microwave CW source as a Movable on frequency or output level. Contracts:
Movable, Configurable (+ `prepare()`).

**Verification status: queries only.** `*IDN?`, `FREQ?`, `POW?`, `OUTP?` and
`SYST:ERR?` replies were read from a real 2420B (fw V4.33b, GPIB,
2026-10-16). The write syntax (`FREQ <Hz>`, `POW <dBm>`, `OUTP ON|OFF`) is
SCPI-style and not yet sent to the instrument.

## Station config

```toml
[devices.rf]
driver        = "gigatronics_2420b"
resource      = "GPIB0::6::INSTR"
axis          = "freq"        # freq (Hz) | power (dBm)
limits        = [1e9, 2e10]   # Hz, or dBm for axis = "power"
power_dbm     = -20.0         # fixed level while sweeping frequency
freq_hz       = 5e9           # fixed frequency while sweeping power
max_power_dbm = 0.0           # hard cap, checked before RF can turn on
settle_s      = 0.05
```

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `*IDN?`; `OUTP OFF`; `POW <dBm>` (freq axis) or `FREQ <Hz>` (power axis); `POW?`. Refuses if the level is above `max_power_dbm` |
| `prepare()` | `OUTP ON` |
| `set(x)` | `FREQ <Hz>` or `POW <dBm>`, then `SYST:ERR?` until clean, then `settle_s` |
| `read()` | `FREQ?`, `POW?` |
| `snapshot()` | `*IDN?`, `FREQ?`, `POW?`, `OUTP?` |
| `safe_state()` | `OUTP OFF` |

Every write is followed by `SYST:ERR?`; a rejected value raises `DriverError`
instead of leaving the old setting in place. `read()` returns `<name>.freq`
(Hz) and `<name>.power` (dBm).

## Quirks and known gaps

- On 2026-10-16 the unit was set to 7.054 GHz at **+20 dBm** (RF off). With
  the default `max_power_dbm = 0` and no `power_dbm`, `configure()` refuses to
  continue until a safe level is set in the station.
- `SYST:ERR?` replies `0, No error` (no quotes, a space after the comma).
- No `*OPC?`: settling is a fixed `settle_s`.
- Modulation, sweep and list modes: not exposed.

## Tests

`tests/test_gpib_adapters.py` (`test_gt_*`).
