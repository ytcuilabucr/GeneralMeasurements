"""YSL Photonics AOTF (gmcore/devices/ysl_aotf.py) and SC-PRO8 laser
(gmcore/devices/ysl_sc_pro8.py), offline: FakeSerial / a fake port stand in for
the COM port. The frames were checked byte for byte against the vendor's
Aotf_control.py and SC_PRO8.py."""

import asyncio
import math

import pytest

from gmcore.devices import DriverError, FakeSerial, SerialBytesTransport, YslAotf, YslScPro8
from gmcore.devices.transport import serial_resource
from gmcore.devices.ysl_aotf import crc16, frame, freq_word, power_word


# -- AOTF ----------------------------------------------------------------------------------
def test_frames_match_the_vendor():
    # turn off, as Aotf_control.py builds it (append_crc of head + 0x40 + tail)
    assert frame(b"\x40").hex(" ") == "a5 5a 5a a5 40 5a a5 a5 5a bb 58"
    assert crc16(b"123456789") == 0xBB3D                 # CRC-16/ARC: poly A001, start 0
    assert freq_word(100.0) == int(100 / 500 * 2**32).to_bytes(4, "big")
    assert power_word(100.0) == (511 + 4096).to_bytes(3, "big")   # 100 % · 10.23 · 0.5
    assert power_word(0.0) == bytes([0x00, 0x10, 0x00])


def test_usable_range_comes_from_the_calibration():
    a = YslAotf("A", FakeSerial())
    lo, hi = a.range_nm
    assert (lo, hi) == (273.0, 1424.0)                   # 20-200 MHz on the vendor's cubic
    assert a.frequency_mhz(lo) == pytest.approx(200, abs=0.1)
    assert a.frequency_mhz(hi) == pytest.approx(20, abs=0.1)
    with pytest.raises(ValueError, match="needs a, b, c, d"):
        YslAotf("A", FakeSerial(), {"a": 1.0})
    with pytest.raises(ValueError, match="never in"):
        YslAotf("A", FakeSerial(), {"a": 0.0, "b": 0.0, "c": 0.0, "d": 500.0})
    with pytest.raises(ValueError, match="max_power"):
        YslAotf("A", FakeSerial(), max_power=0)


def test_aotf_golden_transcript():
    t = FakeSerial(lambda d: b"OKOKOKOK" if d[4] == 0x40 else b"")
    a = YslAotf("A", t, max_power=50.0)
    assert all(math.isnan(v) for v in asyncio.run(a.read()).values())   # unknown at connect
    with pytest.raises(DriverError, match="wavelength first"):
        asyncio.run(a.set_power(10.0))
    assert t.log == []                                   # nothing written on connect

    async def go():
        await a.set(532.0)                               # power unknown: sent as 0
        await a.set_power(25.0)
        await a.set(600.0)                               # keeps 25 %
        await a.safe_state()
    asyncio.run(go())
    f532, f600 = a.frequency_mhz(532.0), a.frequency_mhz(600.0)
    assert t.log == [frame(b"\x01" + freq_word(f532) + power_word(0.0)),
                     frame(b"\x01" + freq_word(f532) + power_word(25.0)),
                     frame(b"\x01" + freq_word(f600) + power_word(25.0)),
                     frame(b"\x40")]
    data = asyncio.run(a.read())
    assert data == {"A.wavelength": 600.0, "A.power": 0.0, "A.rf_frequency": f600}
    with pytest.raises(DriverError, match="outside the calibration"):
        asyncio.run(a.set(1500.0))
    with pytest.raises(DriverError, match="0-50"):
        asyncio.run(a.set_power(60.0))


def test_aotf_turn_off_must_be_acknowledged():
    a = YslAotf("A", FakeSerial(lambda d: b""))
    with pytest.raises(DriverError, match="answered nothing"):
        asyncio.run(a.safe_state())
    assert a.power == 0.0


# -- SC-PRO8 -------------------------------------------------------------------------------
class Laser:
    """Replies as the vendor's SC_PRO8.py expects them."""

    def __init__(self):
        self.state, self.alarm = "READY", "Err00"

    def __call__(self, d: bytes) -> bytes:
        cmd = d.decode().strip()
        if cmd == "STATE?":
            return f"{self.state}\r".encode()
        if cmd == "Er?":
            return f"{self.alarm}\r".encode()
        if cmd == "LA1":
            if self.state != "READY":
                return b"\rERR\r"
            self.state = "Laser On"
            return b"\rOK\rOK\r"
        if cmd == "LA0":
            self.state = "READY" if self.state == "Laser On" else self.state
            return b"\rOK\r"
        if cmd.startswith("fr"):
            return b"OK\r"
        return b"\rOK\r"


def test_laser_golden_transcript():
    dev = Laser()
    t = FakeSerial(dev)
    las = YslScPro8("L", t)
    data = asyncio.run(las.read())
    assert data["L.emission"] == 0.0 and data["L.fault"] == 0.0
    assert math.isnan(data["L.power"]) and math.isnan(data["L.rate"])
    with pytest.raises(DriverError, match="set Power first"):
        asyncio.run(las.set_emission(True))

    async def go():
        await las.set_power(50.0)
        await las.set_power(33.4)                        # nearest 1/6 %: 200/6
        await las.set_rate(0.5)
        await las.set_emission(True)
        return await las.emission_on()
    assert asyncio.run(go())
    asyncio.run(las.safe_state())
    assert [x.decode().strip() for x in t.log] == [
        "STATE?", "Cu300", "Cu200", "fr000500", "LA1", "STATE?", "LA0"]
    assert las.power == pytest.approx(200 / 6) and las.rate_mhz == 0.5
    assert asyncio.run(las.read())["L.emission"] == 0.0


def test_laser_refusals_explain_themselves():
    dev = Laser()
    las = YslScPro8("L", FakeSerial(dev), max_power=20.0)
    with pytest.raises(DriverError, match="0-20"):
        asyncio.run(las.set_power(30.0))
    with pytest.raises(DriverError, match=r"0\.2 MHz is not one of"):
        asyncio.run(las.set_rate(0.2))
    asyncio.run(las.set_power(10.0))
    dev.state, dev.alarm = "FAULT", "Err05"
    with pytest.raises(DriverError, match=r"'LA1' answered \['ERR'\] \(state FAULT, alarm Err05\)"):
        asyncio.run(las.set_emission(True))
    assert asyncio.run(las.read())["L.fault"] == 1.0
    dev.alarm = "Err00"
    assert asyncio.run(las.alarm()) == ""


# -- transport, config ---------------------------------------------------------------------
class Port:
    def __init__(self, reply):
        self.reply, self.sent, self.out = reply, [], b""

    def write(self, data):
        self.sent.append(data)
        self.out += self.reply(data)

    def read_available(self):
        out, self.out = self.out, b""
        return out

    def close(self):
        self.sent.append(b"<closed>")


def test_serial_transport_collects_until_quiet():
    assert serial_resource("com5") == serial_resource("ASRL5") == "ASRL5::INSTR"
    with pytest.raises(ValueError, match="COM<n>"):
        serial_resource("GPIB0::5::INSTR")
    port = Port(lambda d: b"\rOK\rOK\r" if d == b"LA1\r" else b"")
    t = SerialBytesTransport("COM5", port=lambda: port, wait_s=0.05, quiet_s=0.02)
    asyncio.run(t.open())
    port.out = b"stale"                                  # dropped before the write
    assert asyncio.run(t.exchange(b"LA1\r")) == b"\rOK\rOK\r"
    assert asyncio.run(t.exchange(b"X\r")) == b""        # silence: empty after wait_s
    asyncio.run(t.close())
    assert port.sent == [b"LA1\r", b"X\r", b"<closed>"]


def test_station_config(tmp_path):
    from gmcore.config import load_station
    from gmcore.config.registry import check_settings
    from gmcore.measurement import catalog
    p = tmp_path / "s.toml"
    p.write_text('''[station]
name = "optics"
shutdown_order = ["SC", "YAOTF"]

[devices.SC]
driver = "ysl_sc_pro8"
resource = "COM7"
max_power = 20.0

[devices.YAOTF]
driver = "ysl_aotf"
resource = "ASRL8::INSTR"
''')
    cfg = load_station(p)
    assert cfg.devices["SC"].resource == "ASRL7::INSTR"
    assert cfg.devices["YAOTF"].calibration["d"] == 363.79358     # the vendor's cubic
    check_settings(cfg)                                  # builds the drivers, no port opened
    p.write_text(p.read_text().replace("COM7", "GPIB0::7::INSTR"))
    with pytest.raises(ValueError, match="COM<n>"):
        load_station(p)
    a, las = YslAotf("A", FakeSerial()), YslScPro8("L", FakeSerial())
    steps = {c.id: c for c in catalog({"A": a, "L": las}).values() if c.kind == "step"}
    assert steps["A.wavelength"].limits == a.range_nm
    assert {"A.wavelength", "A.power", "L.power"} <= set(steps)
