# Adding an adapter

An adapter is one module in `sweepkit/devices/` that owns one instrument
session and implements the contracts in `devices/protocols.py`.

## Checklist

1. **Transport.** Take a `Transport` (`VisaTransport` for real hardware,
   `FakeTransport` in tests). The transport owns the lock and timeouts; the
   adapter never touches VISA directly. Blocking calls stay in the transport
   (`asyncio.to_thread`).
2. **Contract.** Readable needs `name`, `describe()`, `read()`. Movable adds
   `limits` and `set()` (returns only when settled; enforce limits and ramp
   rate here). Rampable adds `start_ramp(target, rate)` and `ramp_done()`.
3. **Fields.** `describe()` returns `{key: FieldInfo(units, label, shape,
   dtype)}`; keys must be unique across the station (two devices producing the
   same key is rejected at plan time). `read()` returns exactly those keys,
   SI units. Put a `<name>.compliance`-style 0/1 field on anything a watchdog
   should be able to see.
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

```python
class MyDevice:
    def __init__(self, name, transport, limits=(-1.0, 1.0), max_rate=0.1):
        self.name, self.t, self.limits, self.max_rate = name, transport, limits, max_rate

    def describe(self):
        return {f"{self.name}.v": FieldInfo("V", f"{self.name} voltage")}

    async def set(self, value):          # Movable
        if not self.limits[0] <= value <= self.limits[1]:
            raise DriverError(f"{self.name}: {value} outside limits {self.limits}")
        ...                              # ramp at max_rate, wait until settled

    async def read(self):
        return {f"{self.name}.v": float(await self.t.query("MEAS?"))}

    async def safe_state(self):
        ...                              # zero + output off
```
