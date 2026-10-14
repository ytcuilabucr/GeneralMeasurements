# SRS SR830 / SR850 lock-in (`sr830`, `sr850`)

Contract: Readable, Configurable. One shared implementation (`_SRSLockin`)
with thin `SR830` / `SR850` subclasses. Reference frequency and amplitude are
set once at `configure()`; they are not Movable axes yet.

**Verification status: not run against hardware** (fake-transport tests only).
The SR850 shares the SR830 command subset and index tables by assumption.

## Station config

```toml
[devices.lockin]
driver        = "sr830"       # or "sr850"
resource      = "GPIB0::8::INSTR"
frequency     = 17.77         # Hz; omit to leave the reference as is
amplitude     = 0.1           # V rms, SLVL
time_constant = 0.3           # s; nearest table entry is used
sensitivity   = 1e-3          # V full scale; nearest table entry is used
```

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `OUTX 1`; `SLVL <v>`; `FREQ <f>` (if set); `OFLT <i>`; `SENS <i>` |
| `read()` | `SNAP? 1,2,3,4` → X, Y, R, θ sampled simultaneously |
| `read_aux(n)` | `OAUX? <n>` (n = 1..4) |
| `snapshot()` | `*IDN?`, `FREQ?`, `SLVL?`, `PHAS?`, `SENS?`, `OFLT?` |
| `safe_state()` | nothing (leaves the reference running) |

`read()` returns `<name>.x`, `<name>.y`, `<name>.r` (V) and `<name>.theta` (deg).

Index tables: sensitivity index 0 = 2 nV … 26 = 1 V in a 1-2-5 sequence;
time-constant index 0 = 10 µs … 19 = 30 ks (1-3 sequence).

## Quirks and known gaps

- **No overload / unlock detection.** `LIAS?` is not polled, so a saturated
  input still returns numbers. A watchdog on the status byte is the obvious
  next addition.
- No auto-sensitivity, auto-phase, filter slope, sync filter, harmonic or
  reference-source selection: set these on the front panel or extend
  `configure()`.
- The time-constant is not waited for: use `Axis(settle_s=...)` of ~5-10 × the
  time constant after moving the swept parameter.
- Nearest-table rounding is silent; read it back from `snapshot()`.
- Aux outputs (`AUXV`) are not driven.

## Tests

`tests/test_devices.py` (SNAP parsing, index mapping for both classes).
