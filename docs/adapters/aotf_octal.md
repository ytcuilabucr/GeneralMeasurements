# Crystal Technology octal AOTF controller (`aotf_octal`)

`gmcore/devices/aotf_octal.py`. This is the AOTF supplied with the Fianium
supercontinuum source, an Octal Channel AOTF Controller on USB (VID 16C7,
PID 0001). One instrument entry controls **one RF channel**: its **wavelength**
and its **power**, and nothing else.

**Status:** written from the vendor's documentation (`C:\Program Files\Crystal
Technology\Documentation\AOTF Controller Command Reference.pdf`, rev. 1.3, and
`Developer\Documentation\AOTF Controller Communication.pdf`), the library
header `AotfLibrary.h`, the lab's LabVIEW program (`AOTF Labview/`, 2010), and
the calibration files.

- **Checked here:** the 32-bit `AotfLibrary.dll` loads through the bridge, and
  `AotfOpen` answers (no controller at a free instance).
- **Not verified on hardware:** the controller was not connected. The first
  session should check:
  - the reply format of `dds a <ch>` (the parser takes the last integer);
  - whether the controller echoes commands and prompts with `*` (the
    transport copes with either);
  - one wavelength against a spectrometer.

## Station config

```toml
[devices.AOTF]
driver = "aotf_octal"
resource = "AOTF::0"                 # USB instance: 0 for the first controller
channel = 0                          # 0-7
mode = "vis"                         # which calibration: "vis" or "nir"
calibrations = { vis = "Instrument config/aotf/102102 Visible.aotf", nir = "Instrument config/aotf/102102 NIR1.aotf" }
max_power = 100.0                    # % cap (default 100)
# settle_s = 0.0                     # wait after each set
# dll = 'C:\Program Files (x86)\Fianium\config files\AotfLibrary.dll'
```

There are **no wavelength limits** in the config. The Instrument Server's
driver menu lists it under **Crystal Technology (Fianium)**. A newly picked
AOTF starts in VIS with both calibration files filled in and `max_power = 10`.

## Mode: the VIS or NIR calibration

The same controller drives the visible or the NIR crystal. Which one is set
only by the calibration file, as with the LabVIEW program's "tuning curve"
selector (visible / NIR1 / NIR2). The driver's mode is that choice:

| Mode | Label | Calibration | Usable range (from the file) |
| --- | --- | --- | --- |
| `vis` | Visible (VIS) | `102102 Visible.aotf` | 370-984 nm |
| `nir` | Near infrared (NIR) | `102102 NIR1.aotf` | 370-1579 nm |

- **Switch mode…** in the panel switches in one click: no dialog, no
  confirmation, no interlock, also with the light on.
- Nothing is sent to the controller. The RF frequency and amplitude stay; the
  panel re-reads them and shows the wavelength and power they mean on the new
  curve (power is relative to the new file's PowerNorm band).
- As with the Keithley source function, the switch lasts until the GUI closes;
  the station file keeps its `mode`.

**Usable range.** Not a setting: it is where the calibration file can be used
at all, worked out when the instrument is built. It is the span of the file's
PowerNorm bands (370-2300 nm in both), cut to where the curve's RF stays in
the controller's 1-200 MHz. The curve must be monotonic there (both are), so
each frequency reads back as one wavelength. Wavelengths outside it are
refused because there is no RF frequency for them. Inside it the cubic is
used as it is, even where it extrapolates past the crystal's real working
band. For reference, the LabVIEW program's `get limits.vi` uses visible
400-700, NIR1 600-1100 and NIR2 1100-1650 nm.
- There is no NIR2 calibration for crystal 102102 on this PC (only the vendor's
  all-1 placeholder), so NIR2 is not offered.

## How it talks: USB through the vendor's 32-bit DLL

The controller has no VISA interface. The vendor's `AotfLibrary.dll` (`AotfOpen`,
`AotfWrite`, `AotfRead`, `AotfIsReadDataAvailable`, `AotfClose`) is
**32-bit only**, and this program is 64-bit. **msl-loadlib**
(`pip install msl-loadlib`, or the `aotf` extra) runs the DLL in its own 32-bit
server process (`gmcore/devices/aotf_server32.py`), and `AotfLibraryTransport`
calls it. Starting the server takes about a second, at connect.

Commands are ASCII lines ending in CR. A reply ends at the controller's `*`
prompt, or after 50 ms of quiet; an echo of the command is dropped. A write
whose output reads like an error (`invalid`, `unknown`, `error`, ...) raises.

Only one program can hold the controller: close Fianium's "AOTF Controller"
program and the LabVIEW VI first.

## Commands used

These are the LabVIEW program's own formats (`format cmd.vi`,
`turn all off.vi`, `find all AOTFs.vi`, `get all parameters.vi`):

| Purpose | Command | Notes |
| --- | --- | --- |
| RF frequency | `dds f <ch> <MHz>` | `%f`, MHz |
| amplitude | `dds a <ch> <asf>` | 0 … 16383; 0 = no RF, no light |
| controller drives the modulation | `dau en`, then `mod dac <ch> 16383` | sent once, before the first power above 0 (the VI sends them at start, for all channels) |
| read back (on connect) | `dds f <ch>`, `dds a <ch>` | queries; reply e.g. `Channel 0 profile 0 frequency 1.153e+08Hz (Ftw …)` |
| serial number (snapshot) | `boardid serial` | |

Nothing is written on connect.

## Wavelength

The calibration file's `[Calibration] tuning0-3` give the RF frequency:

    f [MHz] = tuning0 + tuning1·λ + tuning2·λ² + tuning3·λ³,  λ in nm

For the lab's visible crystal (serial 102102, the same coefficients as
`D:\Documents\Fianium\AOTF\14513A860600002E visible.aotf`) this gives 146.1 MHz
at 450 nm, 115.3 MHz at 532 nm and 81.7 MHz at 700 nm. The NIR1 file gives
99.6 MHz at 600 nm and 50.1 MHz at 1100 nm.

The driver computes the frequency and sends MHz, as the VI's "legacy" path does.
It does not use `dds f <ch> #<nm>`, which would apply whatever coefficients are
stored in the controller. `__init__` refuses a calibration whose curve isn't
monotonic over its usable range, never reaches 1-200 MHz, or has a gap in its
power bands. It also refuses the vendor's "default" files, whose coefficients
are all 1.

## Power

Power is in **% of the calibration's PowerNorm** for the wavelength's band:

    PowerNorm = Power Opt:370-600=6240,600-670=6400,670-740=8600,...

That is the amplitude that gives the optimum diffracted power in each band. At
100 %, the driver sends exactly that amplitude, never more (the controller
itself would take up to 16383). Changing the wavelength with power on rescales
the amplitude for the new band. The lower of the old and new amplitude is
written first, so no transient goes above either setting. `max_power` caps
the percentage.

## Control only: readings are done in software

The controller only generates RF. It has **no optical detector** in this setup.
There is an optional optical-power-sensor input (the Light Intensity Tracking
feature), but the LabVIEW program never reads it (no `adc` commands). So:

- `read()` reports what the driver set, with no bus traffic:
  - `<name>.wavelength` (nm)
  - `<name>.power` (%)
  - `<name>.rf_frequency` (MHz, level 1)
- On connect, `sync()` reads the channel's present frequency and amplitude back
  (queries only), so the readings start from the real state. A frequency
  outside the configured range, or a reply that can't be parsed, leaves them
  unknown (NaN), and power can't be set until a wavelength is.
- **Update settings** in the panel runs `sync()` again.

## Safe state

`dds a <ch> 0`: no RF on this channel, so no light. The frequency is kept.
Other channels are not touched.

## Tests

`tests/test_aotf.py`:

- calibration parsing, including placeholder and broken files;
- the settings checks;
- read-back on connect;
- the golden command transcript (including the enable step and the amplitude
  ordering on a band change);
- the usable range, the VIS/NIR modes and one-click switching;
- the power cap;
- the transport, with a fake port that echoes and prompts;
- the station config;
- the channels General Measurement offers;
- the real DLL through the bridge (skipped where it isn't installed).
