"""Crystal Technology octal AOTF controller (gmcore/devices/aotf_octal.py) and its
USB transport, offline: a fake port stands in for AotfLibrary.dll."""

import asyncio
import math
from pathlib import Path

import pytest

from gmcore.devices import AotfLibraryTransport, AotfOctal, DriverError, FakeTransport
from gmcore.devices.aotf_octal import read_calibration

VISIBLE = "Instrument config/aotf/102102 Visible.aotf"
NIR1 = "Instrument config/aotf/102102 NIR1.aotf"


def _cals():
    return {"vis": read_calibration(VISIBLE), "nir": read_calibration(NIR1)}


def _aotf(t=None, **kw):
    return AotfOctal("AOTF", t or FakeTransport(lambda c: ""), _cals(), **kw)


# -- calibration ---------------------------------------------------------------------------
def test_calibration_files_are_read():
    c = read_calibration(VISIBLE)
    assert c.tuning == (772.6, -2.657, 3.575e-3, -1.699e-6)
    assert c.bands[0] == (370.0, 600.0, 6240) and c.norm(700) == 8600
    assert c.frequency(450) == pytest.approx(146.066, abs=1e-3)       # TeO2, visible
    assert read_calibration(NIR1).frequency(1100) == pytest.approx(50.055, abs=1e-3)


def test_placeholder_and_broken_calibrations_are_refused(tmp_path):
    p = tmp_path / "default visible.aotf"           # as the Fianium "default" files ship
    p.write_text('[Calibration]\ntuning0=1\ntuning1=1\ntuning2=1\ntuning3=1\n\n'
                 'PowerNorm="Power:370-2300=4915"\n')
    with pytest.raises(ValueError, match="placeholder"):
        read_calibration(p)
    p.write_text("[Calibration]\ntuning0=1\n")
    with pytest.raises(ValueError, match="tuning1"):
        read_calibration(p)
    with pytest.raises(ValueError, match="calibration"):
        read_calibration(tmp_path / "missing.aotf")


def test_settings_are_checked_before_any_io(tmp_path):
    with pytest.raises(ValueError, match="0-7"):
        _aotf(channel=8)
    with pytest.raises(ValueError, match="max_power"):
        _aotf(max_power=150)
    d = _aotf()
    assert d.wavelength_of(d.frequency_mhz(532.0)) == pytest.approx(532.0, abs=1e-6)
    assert d.wavelength_of(500.0) is None           # 500 MHz: not on this curve
    p = tmp_path / "bumpy.aotf"                      # a curve that turns back
    p.write_text('[Calibration]\ntuning0=150\ntuning1=-0.2\ntuning2=2e-4\ntuning3=0\n'
                 'PowerNorm="Power:370-1000=5000"\n')
    with pytest.raises(ValueError, match="not monotonic"):
        AotfOctal.usable_range(read_calibration(p))


def test_the_usable_range_comes_from_the_calibration():
    # inside the PowerNorm bands (370-2300 nm), where the RF stays in 1-200 MHz
    vis, nir = _aotf(), _aotf(mode="nir")
    assert vis.range_nm[0] == nir.range_nm[0] == 370.0
    assert vis.frequency_mhz(vis.range_nm[1]) == pytest.approx(1.0, abs=0.1)
    assert nir.frequency_mhz(nir.range_nm[1]) == pytest.approx(1.0, abs=0.2)
    assert 980 < vis.range_nm[1] < 985 and 1575 < nir.range_nm[1] < 1580


# -- commands ------------------------------------------------------------------------------
def test_connect_reads_the_present_state_back():
    f = AotfOctal.frequency_mhz(_aotf(), 532.0) * 1e6
    t = FakeTransport({"dds f 0": f"Channel 0 profile 0 frequency {f:.6e}Hz (Ftw 123)",
                       "dds a 0": "Channel 0 amplitude 3120"})
    d = _aotf(t)
    asyncio.run(d.sync())
    assert t.log == ["dds f 0", "dds a 0"]          # queries only
    assert d.wavelength == pytest.approx(532.0, abs=1e-3) and d.power == pytest.approx(50.0)
    data = asyncio.run(d.read())
    assert data["AOTF.wavelength"] == pytest.approx(532.0, abs=1e-3)
    assert data["AOTF.rf_frequency"] == pytest.approx(f / 1e6, rel=1e-6)


def test_unknown_state_until_set():
    t = FakeTransport({"dds f 0": "garbage", "dds a 0": "?"})
    d = _aotf(t)
    asyncio.run(d.sync())
    assert d.wavelength is None and d.power is None
    assert all(math.isnan(v) for v in asyncio.run(d.read()).values())
    with pytest.raises(DriverError, match="wavelength first"):
        asyncio.run(d.set_power(10))
    assert t.log == ["dds f 0", "dds a 0"]          # refused before any write


def test_golden_transcript_wavelength_and_power():
    t = FakeTransport(lambda c: "")
    d = _aotf(t, channel=2)

    async def go():
        await d.set(532.0)
        await d.set_power(50.0)                      # first light: enable, then amplitude
        await d.set_power(25.0)                      # no second enable
        await d.set(650.0)                           # band 600-670: 6400 > 6240, freq first
        await d.set(560.0)                           # back to 6240: amplitude first
        await d.safe_state()
    asyncio.run(go())
    f532, f650, f560 = (f"{d.frequency_mhz(x):.6f}" for x in (532.0, 650.0, 560.0))
    assert t.log == [f"dds f 2 {f532}", "dau en", "mod dac 2 16383", "dds a 2 3120",
                     "dds a 2 1560",
                     f"dds f 2 {f650}", "dds a 2 1600",
                     "dds a 2 1560", f"dds f 2 {f560}",
                     "dds a 2 0"]
    assert (d.power, d.asf, d.wavelength) == (0.0, 0, 560.0)


def test_limits_and_power_cap():
    t = FakeTransport(lambda c: "")
    d = _aotf(t, max_power=20.0)
    with pytest.raises(DriverError, match=r"outside the 102102 Visible.aotf calibration"):
        asyncio.run(d.set(1200.0))
    asyncio.run(d.set(500.0))
    with pytest.raises(DriverError, match="0-20"):
        asyncio.run(d.set_power(50.0))
    asyncio.run(d.set_power(20.0))
    assert t.log[-1] == "dds a 0 1248"               # 20 % of 6240, never above PowerNorm


# -- transport -----------------------------------------------------------------------------
class FakePort:
    """A controller that echoes, answers, and prompts with "*"."""

    def __init__(self, replies):
        self.replies, self.sent, self.out = replies, [], b""

    def write(self, data):
        self.sent.append(data)
        cmd = data.decode().strip()
        self.out += data.replace(b"\r", b"\r\n") + self.replies.get(cmd, b"") + b"\r\n* "

    def read_available(self):
        out, self.out = self.out, b""
        return out

    def close(self):
        self.sent.append(b"<closed>")


def _transport(replies):
    port = FakePort(replies)
    t = AotfLibraryTransport(port=lambda: port, timeout_s=0.3, quiet_s=0.02, idle_s=0.05)
    asyncio.run(t.open())
    return t, port


def test_transport_strips_echo_and_prompt():
    t, port = _transport({"dds f 0": b"Channel 0 profile 0 frequency 1.0e+08Hz (Ftw 1)\r\n"})
    assert asyncio.run(t.query("dds f 0")) == "Channel 0 profile 0 frequency 1.0e+08Hz (Ftw 1)"
    asyncio.run(t.write("dds a 0 0"))
    assert port.sent == [b"dds f 0\r", b"dds a 0 0\r"]
    asyncio.run(t.close())
    assert port.sent[-1] == b"<closed>"


def test_transport_errors_and_silence():
    t, _ = _transport({"dds q": b"Invalid command: dds q\r\n"})
    with pytest.raises(DriverError, match="Invalid command"):
        asyncio.run(t.write("dds q"))

    class Silent(FakePort):
        def write(self, data):
            self.sent.append(data)
    port = Silent({})
    t = AotfLibraryTransport(port=lambda: port, timeout_s=0.1, idle_s=0.05)
    asyncio.run(t.open())
    asyncio.run(t.write("dds a 0 0"))                # a write may stay silent
    with pytest.raises(TimeoutError, match="no reply"):
        asyncio.run(t.query("dds f 0"))


@pytest.mark.skipif(not Path(r"C:\Program Files (x86)\Fianium\config files\AotfLibrary.dll")
                    .is_file(), reason="the vendor's AotfLibrary.dll is not installed")
def test_the_32_bit_library_loads_and_finds_no_controller_at_a_free_instance():
    pytest.importorskip("msl.loadlib")
    t = AotfLibraryTransport(instance=99)            # AotfOpen(99): no such controller
    with pytest.raises(DriverError, match="no AOTF controller at USB instance 99"):
        asyncio.run(t.open())


# -- configuration and the GUI's view of it ------------------------------------------------
def test_station_config_and_channels(tmp_path):
    from gmcore.config import load_station
    from gmcore.config.registry import check_settings
    from gmcore.measurement import catalog
    p = tmp_path / "s.toml"
    p.write_text(f'''[station]
name = "optics"
shutdown_order = ["AOTF"]

[devices.AOTF]
driver = "aotf_octal"
resource = "AOTF::0"
calibrations = {{ vis = "{Path(VISIBLE).resolve().as_posix()}", nir = "{Path(NIR1).resolve().as_posix()}" }}
max_power = 10.0
''')
    cfg = load_station(p)
    check_settings(cfg)                              # builds the driver, no USB
    good = p.read_text()
    p.write_text(good.replace("max_power", "limits = [450.0, 700.0]\nmax_power"))
    with pytest.raises(ValueError, match="limits"):  # no wavelength limits any more
        load_station(p)
    bad = good.replace("AOTF::0", "GPIB0::1::INSTR")
    p.write_text(bad)
    with pytest.raises(ValueError, match="AOTF::"):
        load_station(p)
    d = _aotf()
    assert set(d.describe()) == {"AOTF.wavelength", "AOTF.power", "AOTF.rf_frequency"}
    steps = {c.id: c for c in catalog({"AOTF": d}).values()
             if c.kind == "step" and c.instrument == "AOTF"}       # not the Timer's
    assert set(steps) == {"AOTF.wavelength", "AOTF.power"}
    assert steps["AOTF.wavelength"].limits == d.range_nm
    assert steps["AOTF.power"].limits == (0.0, 100.0)


# -- calibration mode (VIS / NIR) ----------------------------------------------------------
def test_mode_picks_the_calibration():
    d = _aotf(mode="nir")
    assert d.cal_name == "102102 NIR1.aotf"
    assert d.frequency_mhz(1100.0) == pytest.approx(50.055, abs=1e-3)
    with pytest.raises(ValueError, match="no calibration"):
        AotfOctal("AOTF", FakeTransport(lambda c: ""), {"vis": read_calibration(VISIBLE)},
                  mode="nir")
    with pytest.raises(ValueError, match="mode 'uv'"):
        _aotf(mode="uv")


def test_switch_mode_has_no_interlock_and_writes_nothing():
    t = FakeTransport(lambda c: "")
    d = _aotf(t)

    async def go():
        await d.set(532.0)
        await d.set_power(10.0)                      # light on: switching still allowed
    asyncio.run(go())
    n = len(t.log)
    asyncio.run(d.switch_mode("nir"))
    assert len(t.log) == n                           # software only
    assert (d.mode, d.cal_name, d.wavelength) == ("nir", "102102 NIR1.aotf", None)
    asyncio.run(d.set(800.0))
    # 10 % of NIR1's 740-810 nm PowerNorm (4900), lower than the 624 on: amplitude first
    assert t.log[-2:] == ["dds a 0 490",
                          f"dds f 0 {read_calibration(NIR1).frequency(800.0):.6f}"]
    asyncio.run(d.switch_mode("vis"))
    assert d.mode == "vis" and d.range_nm == _aotf().range_nm


def test_definition_modes():
    from gmcore.devices.definitions import definition_of
    d = _aotf()
    defn = definition_of(d)
    assert defn.mode == "vis" and set(defn.modes) == {"vis", "nir"}
    assert defn.switch == "switch_mode"
    assert not (defn.switch_interlock or defn.switch_settings or defn.switch_confirm)
    d.mode = "nir"
    assert definition_of(d).mode == "nir"
