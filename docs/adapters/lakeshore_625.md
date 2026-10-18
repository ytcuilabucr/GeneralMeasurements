# Lake Shore 625 magnet power supply (`lakeshore_625`)

Superconducting-magnet supply as a Rampable, in amps or (with a field
constant) tesla. Contracts: Rampable, Configurable.

**Verification status: queries only.** Reply formats of `*IDN?`, `RDGI?`,
`RDGV?`, `RDGF?`, `SETI?`, `RATE?`, `OPST?`, `PSH?`, `ERST?` and `LIMIT?` were
read from a real 625 (fw 1.3/1.1, GPIB, 2026-10-16). No write (`SETI`,
`RATE`) has been sent to the instrument yet. Test the first moves with the
magnet's leads on a dummy load or with an expert present.

## Station config

```toml
[devices.magnet]
driver            = "lakeshore_625"
resource          = "GPIB0::12::INSTR"
limits            = [-10.0, 10.0]  # A, or T when tesla_per_amp is set
max_rate          = 0.035          # A/s or T/s
tesla_per_amp     = 0.1            # omit to sweep in amps
persistent_switch = true           # refuse moves while the switch heater is off
tolerance_a       = 0.002          # A, for ramp_done()
```

Line termination is CR LF (set by the registry).

## Safety rules in the driver

- `configure()` is read-only. It refuses the station if `limits` need more
  current than the supply's `LIMIT?` current limit, or `max_rate` exceeds its
  rate limit. (On 2026-10-16 the supply was set to 49.6 A, 5 V, 1 A/s.)
- The driver never writes the persistent-switch heater (`PSH`), the limits
  (`LIMIT`), or the field constant (`FLDS`).
- With `persistent_switch = true`, `start_ramp()` checks `PSH?` and refuses to
  move while the heater is off: the supply would otherwise ramp its leads
  while the magnet stays persistent, and the mismatch can quench the magnet
  when the heater is next turned on.
- `safe_state()` holds the present current (`SETI <RDGI?>`). It never ramps
  the magnet down on its own.

## Commands used

| Step | Commands |
| --- | --- |
| `configure()` | `*IDN?` (must be MODEL625); `LIMIT?`; `SETI?` |
| `start_ramp(x, rate)` | (`PSH?`); `RATE <A/s>`; `SETI <A>` |
| `ramp_done()` | `RDGI?` within `tolerance_a` of the target |
| `set(x)` | `RDGI?`, `start_ramp(x, max_rate)`, poll `ramp_done()` with a timeout of 1.5 x ideal ramp time + 30 s |
| `read()` | `RDGI?`, `RDGV?` |
| `snapshot()` | `*IDN?`, `SETI?`, `RATE?`, `LIMIT?`, `PSH?`, `FLDS?`, `OPST?`, `ERST?` |
| `safe_state()` | `RDGI?`; `SETI <that current>` |

`read()` returns `<name>` (A or T), `<name>.v` (output voltage) and, in tesla
mode, `<name>.i` (A).

## Quirks and known gaps

- Field comes from the driver's own `tesla_per_amp`, not from the
  instrument's `RDGF?`/`FLDS` setting (recorded in the snapshot for
  comparison).
- `OPST?` status bits are recorded but not interpreted: the bit meanings
  still need checking against the manual and the instrument.
- Quench detection is left to the supply; the driver doesn't read `ERST?`
  during a run.

## Tests

`tests/test_gpib_adapters.py` (`test_ls625_*`).
