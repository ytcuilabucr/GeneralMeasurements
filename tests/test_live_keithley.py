"""Live communication tests for the Keithley adapters. Source outputs stay OFF.

Skipped unless SWEEP_LIVE=1. Each instrument is enabled by its own variable
(unset = skipped):

    SWEEP_K2400=GPIB0::26::INSTR  SWEEP_K2401=GPIB0::22::INSTR  SWEEP_K2000=GPIB0::11::INSTR

These tests send *RST and reconfigure the instruments: never run them while a
measurement is using them. Every command and reply is written to
tests/golden/live_<model>.txt as a transcript for the fake-transport tests.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from gmcore.config.registry import DRIVER_CLASSES
from gmcore.devices import DriverError, Keithley2000, VisaTransport

pytestmark = pytest.mark.skipif(os.environ.get("SWEEP_LIVE") != "1",
                                reason="live hardware tests need SWEEP_LIVE=1")

GOLDEN = Path(__file__).parent / "golden"
SMUS = [("SWEEP_K2400", "2400", "volt", 0.1), ("SWEEP_K2401", "2401", "curr", 1e-7)]


class RecordingTransport(VisaTransport):
    """VisaTransport that appends every exchange to a transcript file."""

    def __init__(self, resource: str, model: str, **kw):
        super().__init__(resource, **kw)
        GOLDEN.mkdir(exist_ok=True)
        self._file = GOLDEN / f"live_{model}.txt"

    def _log(self, line: str) -> None:
        with self._file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def write(self, cmd: str) -> None:
        self._log(f"> {cmd}")
        await super().write(cmd)

    async def query(self, cmd: str) -> str:
        t0 = time.perf_counter()
        try:
            reply = await super().query(cmd)
        except Exception as e:
            self._log(f"? {cmd}\n! {type(e).__name__} after {time.perf_counter() - t0:.3f} s")
            raise
        self._log(f"? {cmd}\n< {reply}    ({(time.perf_counter() - t0) * 1e3:.0f} ms)")
        return reply


async def _open(env: str, model: str, **kw) -> RecordingTransport:
    res = os.environ.get(env)
    if not res:
        pytest.skip(f"{env} not set")
    t = RecordingTransport(res, model, **kw)
    await t.open()
    t._log(f"# --- {os.environ.get('PYTEST_CURRENT_TEST', '').split(' ')[0]}")
    return t


def _smu(t, model, mode):
    limits = (-1.0, 1.0) if mode == "volt" else (-1e-6, 1e-6)
    compliance = 1e-6 if mode == "volt" else 0.2
    return DRIVER_CLASSES[f"keithley_{model}"](model, t, mode, compliance, limits, settle_s=0.0)


smu_params = pytest.mark.parametrize(("env", "model", "mode", "level"), SMUS,
                                     ids=[m for _, m, _, _ in SMUS])


# -- SMUs ----------------------------------------------------------------------
@smu_params
async def test_smu_idn_and_clean_queue(env, model, mode, level):
    t = await _open(env, model)
    try:
        assert f"MODEL {model}" in await t.query("*IDN?")
        await t.write("*CLS")
        assert await _smu(t, model, mode).errors() == []
    finally:
        await t.close()


@smu_params
async def test_smu_bad_command_is_reported(env, model, mode, level):
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await t.write("*CLS")
        await t.write(":BOGUS:CMD 1")
        errs = await k.errors()
        assert errs and errs[0].startswith("-113"), errs
        assert await k.errors() == []
    finally:
        await t.close()


@smu_params
async def test_smu_configure_applies_settings(env, model, mode, level):
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await k.configure()                        # raises if any command was rejected
        src, other = ("VOLT", "CURR") if mode == "volt" else ("CURR", "VOLT")
        assert await t.query(":OUTP?") == "0"
        assert await t.query(":SOUR:FUNC?") == src
        assert float(await t.query(f":SENS:{other}:PROT?")) == pytest.approx(k.compliance)
        assert float(await t.query(f":SENS:{other}:NPLC?")) == pytest.approx(1.0)
        assert await t.query(":FORM:ELEM?") == "VOLT,CURR"
    finally:
        await t.close()


@smu_params
async def test_smu_ramp_with_output_off(env, model, mode, level):
    """set() ramp steps are accepted and land exactly; the output never turns on."""
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await k.configure()
        src = "VOLT" if mode == "volt" else "CURR"
        for target in (level, -level, 0.0):
            await k.set(target)
            assert float(await t.query(f":SOUR:{src}:LEV?")) == pytest.approx(target, abs=1e-12)
            assert await t.query(":OUTP?") == "0"
        assert await k.errors() == []
    finally:
        await k.safe_state()
        await t.close()


@smu_params
async def test_smu_read_with_output_off_fails_fast(env, model, mode, level):
    """A raw :READ? with the output off logs 803 and never replies (4 s VISA
    timeout before the fix); read() must refuse at once and send nothing."""
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await k.configure()
        t0 = time.perf_counter()
        with pytest.raises(DriverError, match="output is off"):
            await k.read()
        assert time.perf_counter() - t0 < 0.1
        assert await k.errors() == []
    finally:
        await t.close()


@smu_params
async def test_smu_raw_read_timeout_recovers(env, model, mode, level):
    """The raw failure case: the transport must turn it into TimeoutError and
    leave the session usable (device clear), with no stray reply bytes."""
    t = await _open(env, model, timeout_ms=1000)
    try:
        k = _smu(t, model, mode)
        await k.configure()
        with pytest.raises(TimeoutError):
            await t.query(":READ?")
        assert (await t.query("*IDN?")).startswith("KEITHLEY INSTRUMENTS")
        await t.write("*CLS")                      # the 803 from the raw :READ?
        assert await k.errors() == []
    finally:
        await t.close()


@smu_params
async def test_smu_compliance_query_and_snapshot(env, model, mode, level):
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await k.configure()
        assert (await t.query(k.trip_query)).strip() in ("0", "1")
        snap = await k.snapshot()
        assert f"MODEL {model}" in snap["idn"]
        assert await k.errors() == []
    finally:
        await t.close()


@smu_params
async def test_smu_burst_queries(env, model, mode, level):
    t = await _open(env, model)
    try:
        src = "VOLT" if mode == "volt" else "CURR"
        await _smu(t, model, mode).configure()
        t0 = time.perf_counter()
        replies = [await t.query(":SOUR:FUNC?") for _ in range(50)]
        dt = (time.perf_counter() - t0) / 50
        t._log(f"# 50 x :SOUR:FUNC?: mean {dt * 1e3:.1f} ms")
        assert replies == [src] * 50
    finally:
        await t.close()


@smu_params
async def test_smu_concurrent_queries_stay_paired(env, model, mode, level):
    """The transport lock must keep every reply with its own query."""
    t = await _open(env, model)
    try:
        await _smu(t, model, mode).configure()
        src = "VOLT" if mode == "volt" else "CURR"
        cmds = ["*IDN?", ":SOUR:FUNC?", ":OUTP?"] * 10
        replies = await asyncio.gather(*(t.query(c) for c in cmds))
        for c, r in zip(cmds, replies):
            expected = {"*IDN?": f"MODEL {model}", ":SOUR:FUNC?": src, ":OUTP?": "0"}[c]
            assert expected in r, (c, r)
    finally:
        await t.close()


@smu_params
async def test_smu_cancelled_query_keeps_session_in_sync(env, model, mode, level):
    """Abort cancels an in-flight query; the next query must get its own reply."""
    t = await _open(env, model)
    try:
        for _ in range(5):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(t.query("*IDN?"), 0.001)
            assert await t.query(":OUTP?") == "0"
    finally:
        await t.close()


@smu_params
async def test_smu_safe_state(env, model, mode, level):
    t = await _open(env, model)
    try:
        k = _smu(t, model, mode)
        await k.configure()
        await k.set(level)
        await k.safe_state()
        src = "VOLT" if mode == "volt" else "CURR"
        assert float(await t.query(f":SOUR:{src}:LEV?")) == 0.0
        assert await t.query(":OUTP?") == "0"
    finally:
        await t.close()


# -- Keithley 2000 -------------------------------------------------------------
async def test_k2000_idn_and_bad_command():
    t = await _open("SWEEP_K2000", "2000")
    try:
        k = Keithley2000("dmm", t)
        assert "MODEL 2000" in await t.query("*IDN?")
        await t.write("*CLS")
        await t.write(":BOGUS:CMD 1")
        errs = await k.errors()
        assert errs and errs[0].startswith("-113"), errs
        assert await k.errors() == []
    finally:
        await t.close()


@pytest.mark.parametrize("function", ["volt", "res", "fres"])
async def test_k2000_configure_and_read(function):
    t = await _open("SWEEP_K2000", "2000")
    try:
        k = Keithley2000("dmm", t, function=function, nplc=1)
        await k.configure()
        scpi = {"volt": "VOLT:DC", "res": "RES", "fres": "FRES"}[function]
        assert (await t.query(":SENS:FUNC?")).strip('"') == scpi
        values = [(await k.read())["dmm"] for _ in range(5)]
        t._log(f"# {function} readings: {values}")
        assert all(isinstance(v, float) for v in values)
        assert await k.errors() == []
    finally:
        await t.close()


async def test_k2000_read_timing_by_nplc():
    t = await _open("SWEEP_K2000", "2000", timeout_ms=10000)
    try:
        for nplc in (0.1, 1, 10):
            k = Keithley2000("dmm", t, function="volt", nplc=nplc, range_=10.0)
            await k.configure()
            t0 = time.perf_counter()
            await k.read()
            t._log(f"# :READ? at NPLC {nplc}: {(time.perf_counter() - t0) * 1e3:.0f} ms")
        assert await k.errors() == []
    finally:
        await t.close()


async def test_k2000_timeout_then_next_query_in_sync():
    """A reply that arrives after the VISA timeout must not be read by the next query."""
    t = await _open("SWEEP_K2000", "2000", timeout_ms=10000)
    try:
        k = Keithley2000("dmm", t, function="volt", nplc=10, range_=10.0)
        await k.configure()
        await t.write(":SENS:VOLT:DC:AVER:COUN 10")
        await t.write(":SENS:VOLT:DC:AVER:STAT ON")   # one reading now takes ~2 s
        t._inst.timeout = 300
        with pytest.raises(TimeoutError):
            await t.query(":READ?")
        t._inst.timeout = 10000
        await asyncio.sleep(3)                        # a late reply would be waiting now
        idn = await t.query("*IDN?")
        t._log(f"# first query after timeout returned: {idn!r}")
        assert idn.startswith("KEITHLEY INSTRUMENTS"), f"session out of sync: got {idn!r}"
    finally:
        await t.write(":SENS:VOLT:DC:AVER:STAT OFF")
        await t.close()


async def test_k2000_left_in_fres():
    """Leave the DMM in 4-wire resistance, the mode it was found in."""
    t = await _open("SWEEP_K2000", "2000")
    try:
        await Keithley2000("dmm", t, function="fres", nplc=1).configure()
        assert (await t.query(":SENS:FUNC?")).strip('"') == "FRES"
    finally:
        await t.close()
