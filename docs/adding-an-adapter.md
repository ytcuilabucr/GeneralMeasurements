# Adding an adapter

An adapter is one module in `gmcore/devices/` that owns one instrument
session and implements the contracts in `devices/protocols.py`.

## The rule: one instrument model, one driver

Each instrument model gets its own files, complete on their own:

| What | Where | Example (Keithley 2410) |
| --- | --- | --- |
| Driver | `gmcore/devices/<driver>.py`, one class | `keithley_2410.py`, `Keithley2410` |
| Definition | `gmcore/devices/defs/<driver>.toml` | `keithley_2410.toml` |
| One file per mode (if it has modes) | `defs/<driver>_<mode>.toml` | `keithley_2410_volt.toml`, `keithley_2410_curr.toml` |

- **Nothing shared between models.** A driver imports only the common
  plumbing: `transport`, `protocols` and `definitions`. Never another driver,
  and no base class shared between models.
- **Copy, don't import.** Related models (the Keithley 2400, 2401 and 2410
  speak the same commands) each carry a full copy of their code, with their
  own figures. Changing or testing one model can't break another, and each
  file reads on its own.
- **The model's figures sit in the driver.** That means ranges, output and
  power limits, and allowed values, as class attributes, with the manual they
  came from in the module docstring. The driver checks them in `__init__`,
  before any I/O, so `gm validate` catches them without hardware.
- **Exception:** a driver may serve several models that are the same
  instrument to the program, listed in its definition's `drivers`. The SR830
  and SR850 share `srs_lockin.py` because they have the same command set and
  tables.

Then register the driver:
- `config/schema.py`: its config class;
- `config/registry.py`: its builder and `DRIVER_CLASSES` entry;
- `gui/server/window.py`: its `MODEL_NAMES` entry and its `DRIVER_GROUPS`
  maker, for the Instrument Server's Driver menu. A driver not filed under a
  maker shows up under "Other".

## Checklist

1. **Transport.** Take a `Transport` (`VisaTransport` for real hardware,
   `FakeTransport` in tests). The transport owns the lock and timeouts; the
   adapter never touches VISA directly. Blocking calls stay in the transport
   (`asyncio.to_thread`).
2. **Contract.** Readable needs `name`, `describe()`, `read()`. Movable adds
   `limits` and `set()` (returns only when settled; enforce limits and ramp
   rate here). Rampable adds `start_ramp(target, rate)` and `ramp_done()`.
3. **Properties and fields.** Declare the properties in
   `devices/defs/<driver>.toml` (one file per mode if a setting changes other
   properties' units or roles; see [definitions.md](definitions.md)), set
   `DEFINITION = "<driver>"` on the class, and let `describe()` return
   `definitions.describe(self)`. It yields `{key: FieldInfo(units, label)}`
   for the live properties; keys must be unique across the station (two
   devices producing the same key is rejected at plan time). `read()` returns
   exactly those keys, SI units. Put a `<name>.compliance`-style 0/1 status on
   anything a watchdog should be able to see.
4. **Lifecycle (all optional, all idempotent).**
   - `configure()` once at startup: known state, outputs OFF.
   - `prepare()` before the first move of a run (outputs ON at zero).
   - `snapshot()` returns JSON-able settings (written to the file).
   - `safe_state()` must be safe to call at any time, in any state, after any
     failure; bounded and non-raising where possible. For sources: ramp to
     zero at the device ramp rate, then output off. For magnets: hold.
5. **Errors.** Raise `DriverError` for protocol problems (bad reply,
   instrument error), let `TimeoutError` through. Both are retryable by
   `RetryPolicy`; anything else is treated as a bug and fails the run.
6. **Config.** Add a pydantic model to `config/schema.py` (literal `driver`
   discriminator, `extra="forbid"`) and a branch in `config/registry.py`.
7. **Tests, before touching hardware.** Golden command transcript with
   `FakeTransport`; parsing of normal and garbage replies; limits and
   rate enforcement; `safe_state` ordering; a fault-injection case via
   `FaultyDevice`.
8. **Hardware test.** Gated by `SWEEP_LIVE=1`, dummy load or resistor only,
   never a sample. A bug found on hardware first gets a test that reproduces
   it, then the fix.
9. **Docs.** One page in `docs/adapters/`: station config, commands used,
   quirks, safe state, verification status.

## Skeleton

```toml
# devices/defs/my_device.toml
drivers = ["my_device"]
label = "My source"
safe = "Zero, then output off"

[properties.v]                      # read() key "<name>.v"
level = 0                           # 0 essential, 1 expanded, 2 all
label = "{name} voltage"
unit = "V"

[properties.level]
level = 0
access = "both"
label = "Level"
unit = "V"
get = "level"
set = "set"
readback = "v"
limits = "limits"
ramp = { max_rate = "max_rate", unit = "V/s" }
stoppable = true
```

```python
class MyDevice:
    DEFINITION = "my_device"

    def __init__(self, name, transport, limits=(-1.0, 1.0), max_rate=0.1):
        self.name, self.t, self.limits, self.max_rate = name, transport, limits, max_rate
        self.level = 0.0

    def describe(self):
        return definitions.describe(self)   # {"<name>.v": FieldInfo("V", "<name> voltage")}

    async def set(self, value):          # Movable
        if not self.limits[0] <= value <= self.limits[1]:
            raise DriverError(f"{self.name}: {value} outside limits {self.limits}")
        ...                              # ramp at max_rate, wait until settled

    async def read(self):
        return {f"{self.name}.v": float(await self.t.query("MEAS?"))}

    async def safe_state(self):
        ...                              # zero + output off
```
