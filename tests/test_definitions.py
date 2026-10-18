"""Driver definitions (devices/defs/*.toml): loading, modes, validation, and that
every name a definition uses exists on its driver."""

import re

import pytest

from generalmeasurements.devices import (LM510, SR830, SR850, FakeTransport, Gigatronics2420B,
                                         Keithley24xx, Keithley2000, LakeShore335, LakeShore625)
from generalmeasurements.devices import definitions as defs
from generalmeasurements.devices.definitions import Definition, DefinitionError
from generalmeasurements.devices.transport import DriverError


class Rec(FakeTransport):
    """FakeTransport that also lists the writes on their own."""

    def __init__(self, replies=None):
        super().__init__(replies)
        self.writes: list[str] = []

    async def write(self, cmd):
        self.writes.append(cmd)
        await super().write(cmd)


def every_device(t):
    """One instance per driver and mode (and per optional property)."""
    return [
        Keithley24xx("gate", t, "2400", "volt", 1e-6, (-1, 1), 0.1),
        Keithley24xx("bias", t, "2401", "curr", 0.2, (-2e-6, 2e-6), 1e-6),
        Keithley24xx("g2", t, "2450", "volt", check_compliance=False),
        *(Keithley2000("dmm", t, f) for f in ("volt", "curr", "res", "fres")),
        SR830("li", t), SR850("li2", t),
        Gigatronics2420B("rf", t), Gigatronics2420B("rf", t, "power", (-30, 0)),
        LakeShore335("temp", t, "B", 2, (2, 300), 0.03),
        LakeShore625("magnet", t, (-10, 10), 0.035),
        LakeShore625("magnet", t, (-1, 1), 0.0035, tesla_per_amp=0.1),
        LM510("helium", t, 2, "cm"),
    ]


@pytest.mark.parametrize("dev", every_device(FakeTransport()), ids=lambda d: f"{d.name}")
def test_every_name_in_a_definition_exists_on_its_driver(dev):
    defn = defs.definition_of(dev)
    assert type(dev).__module__.rsplit(".", 1)[1] in (*defn.drivers, "srs_lockin")
    methods = [defn.attach, defn.check_mode, defn.switch]
    for pid, p in defs.props(defn, dev).items():
        methods += [p.set, p.get and p.get.split(".")[0], p.choices_attr]
        if p.ramp:
            methods += [p.ramp.start, p.ramp.stop]
            assert defs.number(p.ramp.max_rate, dev) > 0
        lo, hi = defs.bounds(p, dev)
        assert lo is None or hi is None or lo <= hi, pid
        if p.kind == "enum":
            assert defs.choices(p, dev), pid
        if p.when:
            assert hasattr(dev, p.when.lstrip("!"))
    for m in filter(None, methods):
        assert hasattr(dev, m), f"{type(dev).__name__} has no {m!r}"
    # every template fills in; confirm texts also get the properties' present values
    for template in [defn.label, defn.info, defn.safe,
                     *(t for p in defn.properties.values() for t in (p.label, p.unit, p.tooltip))]:
        assert "{" not in defs.text(template, dev)
    extra = {pid: "x" for pid in defn.properties} | {"value": "1", "mode": "m", "mode_label": "M"}
    for template in [defn.switch_confirm, *(p.confirm for p in defn.properties.values())]:
        assert "{" not in defs.text(template, dev, **extra)


def test_describe_comes_from_the_definition():
    t = FakeTransport()
    assert Keithley24xx("bias", t, "2401", "curr", 0.2, (-2e-6, 2e-6), 1e-6).describe() == {
        "bias.v": defs.FieldInfo("V", "bias voltage"),
        "bias.i": defs.FieldInfo("A", "bias current"),
        "bias.compliance": defs.FieldInfo("", "bias in compliance (0/1)")}
    assert list(Keithley24xx("g", t, check_compliance=False).describe()) == ["g.v", "g.i"]
    assert Keithley2000("dmm", t, "fres").describe() == {"dmm": defs.FieldInfo("Ohm", "dmm")}
    assert LakeShore335("temp", t, "B").describe()["temp"].label == "temp temperature (input B)"
    assert list(LakeShore625("m", t, (-1, 1), 0.01).describe()) == ["m", "m.v"]
    tesla = LakeShore625("m", t, (-1, 1), 0.01, tesla_per_amp=0.1).describe()
    assert (tesla["m"].units, tesla["m"].label, "m.i" in tesla) == ("T", "m field", True)
    assert LM510("he", t, 2, "cm").describe()["he"] == defs.FieldInfo("cm", "he level (channel 2)")


def test_mode_files_override_field_by_field():
    volt, curr = defs.load("keithley_24xx", "volt"), defs.load("keithley_24xx", "curr")
    assert (volt.properties["level"].unit, volt.properties["level"].readback) == ("V", "v")
    assert (curr.properties["level"].unit, curr.properties["level"].readback) == ("A", "i")
    assert (volt.properties["compliance"].unit, curr.properties["compliance"].unit) == ("A", "V")
    assert list(curr.properties)[:2] == ["output", "v"]          # main file's order first
    rng = defs.load("keithley_2000", "res").properties["range"]
    assert (rng.unit, rng.allow_auto, rng.set) == ("Ohm", True, "set_range")   # kept from main
    with pytest.raises(DefinitionError, match="not one of"):
        defs.load("keithley_24xx", "ohms")
    with pytest.raises(DefinitionError, match="no modes"):
        defs.load("lakeshore_335", "volt")


@pytest.mark.parametrize("props, error", [
    ({"a": {"readback": "nope"}}, "readback 'nope' is not a property"),
    ({"go": {"kind": "action"}}, "an action needs `set`"),
    ({"x": {"access": "both"}}, "writable but no `set`"),
    ({"e": {"kind": "enum", "access": "both", "set": "s"}}, "needs `choices`"),
    ({"a": {"key": "v"}, "v": {}}, "duplicate read"),
    ({"Level": {}}, "snake_case"),
    ({"r": {"kind": "enum", "choices": {"Off": 0}, "confirm_when": ["On"]}}, "confirm_when"),
    ({"s": {"kind": "status", "access": "both", "set": "x"}}, "read only"),
    ({"a": {"colour": "red"}}, "Extra inputs"),
])
def test_bad_definitions_are_refused(props, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        Definition.model_validate({"drivers": ["x"], "properties": props})


def test_bad_files_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(defs, "DEFS", tmp_path)
    defs.load.cache_clear()
    try:
        (tmp_path / "dev.toml").write_text('drivers = ["dev"]\nmode_attr = "m"\n'
                                           'modes = { a = "A", b = "B" }\n', "utf-8")
        (tmp_path / "dev_a.toml").write_text('mode = "b"\n', "utf-8")
        with pytest.raises(DefinitionError, match="needs mode = 'a'"):
            defs.load("dev", "a")
        with pytest.raises(DefinitionError, match=re.escape("no definition dev_b.toml")):
            defs.load("dev", "b")
        (tmp_path / "bad.toml").write_text("drivers = [", "utf-8")
        with pytest.raises(DefinitionError, match=re.escape("bad.toml")):
            defs.load("bad")
    finally:
        defs.load.cache_clear()


async def test_reading_the_settings_only_queries():
    replies = {"OUTP?": "0", "SENS?": "17", "OFLT?": "8", "*IDN?": "id", "FREQ?": "1000",
               "SLVL?": "0.1", "PHAS?": "0", "RANGE? 2": "1", "SETP? 2": "4",
               "RAMP? 2": "0,1", "OUTMODE? 2": "1,2,0", "PID? 2": "50,20,0", "SETI?": "0",
               "RATE?": "0.01", "LIMIT?": "49.6,5,1", "PSH?": "0", "FLDS?": "1,0.1",
               "OPST?": "006", "ERST?": "000,000,000", "MODE?": "S", "INTVL?": "00:30:00",
               "LOW?": "5.0 cm", "HIGH?": "40.0 cm", "LNGTH?": "45.0 cm", "POW?": "-20"}
    t = Rec(replies)
    for dev in every_device(t):
        values = await defs.read_connect(defs.definition_of(dev), dev)
        assert t.writes == [], type(dev).__name__
        assert all(v is not None or p == "range" for p, v in values.items())
    li = SR830("li", t)
    values = await defs.read_connect(defs.definition_of(li), li)
    assert values["sensitivity"] == pytest.approx(li.SENSITIVITY[17])
    assert values["frequency"] == 1000.0
    assert t.log.count("SLVL?") == 3          # snapshot() once per lock-in, not per property


async def test_keithley_switch_mode_ramps_down_then_reconfigures():
    t = Rec(lambda cmd: "0," if cmd == ":SYST:ERR?" else "0")
    k = Keithley24xx("gate", t, "2400", "volt", 1e-6, (-1, 1), 10.0, step_dt=0.001,
                     settle_s=0)
    k._output, k._level = True, 0.5
    with pytest.raises(ValueError, match="outside 2400 range"):
        await k.switch_mode("curr", 21.0, (-5.0, 5.0), 1e-6)      # 5 A: refused, no I/O
    assert t.writes == [] and k.mode == "volt"
    await k.switch_mode("curr", 21.0, (-1e-6, 1e-6), 1e-7)
    off = t.writes.index(":OUTP OFF")
    assert t.writes[off - 1] == ":SOUR:VOLT:LEV 0"                # ramped to 0 first
    assert t.writes[off + 1:off + 3] == ["*RST", "*CLS"]
    assert ":SOUR:FUNC CURR" in t.writes and ":SENS:VOLT:PROT 21" in t.writes
    assert (k.mode, k.limits, k.compliance, k.output_on) == ("curr", (-1e-6, 1e-6), 21.0, False)
    assert defs.definition_of(k).properties["level"].unit == "A"


async def test_keithley_2000_settings_go_through_the_driver():
    t = Rec(lambda cmd: "0,\"No error\"" if cmd == ":SYST:ERR?" else "x")
    dmm = Keithley2000("dmm", t, "fres", range_=100.0)
    defn = defs.definition_of(dmm)
    await defs.write(defn, dmm, "nplc", 10.0)
    await defs.write(defn, dmm, "range", None)
    assert t.writes == [":SENS:FRES:NPLC 10", ":SENS:FRES:RANG:AUTO ON"]
    with pytest.raises(DriverError, match="NPLC"):
        await dmm.set_nplc(50)
    await dmm.switch_mode("volt")
    assert ":SENS:FUNC 'VOLT:DC'" in t.writes and dmm.describe()["dmm"].units == "V"


async def test_write_starts_a_ramp_at_the_given_rate():
    replies = {"RDGI?": "0.0", "PSH?": "1"}
    t = Rec(replies)
    m = LakeShore625("magnet", t, (-10, 10), 0.035)
    defn = defs.definition_of(m)
    await defs.write(defn, m, "target", 2.0, 0.01)
    assert t.writes == ["RATE 0.0100", "SETI 2.0000"]
    await defs.write(defn, m, "target", 1.0)                     # no rate: the max rate
    assert t.writes[-2:] == ["RATE 0.0350", "SETI 1.0000"]
    await defs.write(defn, m, "hold")
    assert t.writes[-1] == "SETI 0.0000"
