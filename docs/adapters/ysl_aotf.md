# YSL Photonics AOTF (`ysl_aotf`)

`gmcore/devices/ysl_aotf.py`. The YSL AOTF's RF driver on a serial port. One
instrument entry controls **one diffracted line**: its **wavelength** and its
**power**, and nothing else.

**Status:** written from the vendor's Python driver
(`YSL-AOTF_Py with new driver/Aotf_control.py`, `SerialPort.py`) and the C++
library it mirrors (`CPP/YSLAotfLib/YslAotfLib.cpp`).

- **Checked here:** every frame the driver sends is byte-identical to the vendor
  code's, for a grid of wavelengths and powers, and for turn-off (the vendor
  code was run against a stand-in serial port).
- **Not verified on hardware:** the reply to a setting (the vendor ignores it;
  so does the driver), the light at one wavelength against a spectrometer.

## Station config

```toml
[devices.YAOTF]
driver = "ysl_aotf"
resource = "COM8"                    # or "ASRL8::INSTR"; 9600 8N1
max_power = 10.0                     # % cap (default 100)
# settle_s = 0.0                     # wait after each set
# calibration = { a = -2.09537e-7, b = 6.67185e-4, c = -0.76660, d = 363.79358 }
```

The calibration defaults to the vendor's coefficients in `Aotf_control.py`.
There are no wavelength limits in the config. The server's driver menu lists it
under **YSL Photonics**.

## Protocol

Serial, 9600 baud, 8N1, binary frames:

    A5 5A 5A A5 | body | 5A A5 A5 5A | CRC-16 high, low

The CRC covers everything before it: CRC-16/ARC (poly 0xA001 reflected,
start 0x0000), the vendor's `append_crc`.

| Purpose | Body | Reply |
| --- | --- | --- |
| set one line | `01` + frequency word (4 bytes) + power word (3 bytes) | not documented, ignored |
| turn off | `40` | `OKOKOKOK` (8 bytes), checked |

- **Frequency word:** `int(MHz / 500 · 2³²)`, big-endian. The vendor's range
  is 20-200 MHz.
- **Power word:** `int(% · 10.23 · factor) + 4096`. The factor depends on how
  many lines are set at once; for one line it is 0.5, so 100 % sends 4607.

Not used: the vendor's multi-line frames (2-8 lines in one frame, with
per-line power factors) and its sweep commands (`0x11`, `0x13`).

The transport (`SerialBytesTransport`) drops stale input, writes the frame,
and collects the reply until the port has been quiet for 0.15 s, or waits 1 s
when nothing comes.

## Wavelength

    f [MHz] = a·λ³ + b·λ² + c·λ + d,  λ in nm

**Usable range** (not a setting): where the curve stays in 20-200 MHz, worked
out when the instrument is built. The curve must be monotonic there. With the
vendor's coefficients this is **273-1424 nm** (200 MHz at 273 nm, 20 MHz at
1424 nm). Inside it the cubic is used as it is, even past what the crystal
really covers.

## Control only: readings are done in software

The controller has **no query command**. Nothing can be read back, not even on
connect:

- `read()` reports what the driver set: `<name>.wavelength` (nm),
  `<name>.power` (%), `<name>.rf_frequency` (MHz, level 1).
- After connect all three are unknown (NaN). Nothing is written on connect.
- Each frame carries both frequency and power. The first **Wavelength** set
  sends power 0 unless a power was set before. **Power** needs a wavelength
  first.

## Safe state

The vendor's turn-off frame (`40`): no RF, no light. The reply must be
`OKOKOKOK`, otherwise it raises (power is still recorded as 0).

## Tests

`tests/test_ysl.py`:

- frames, including the vendor's turn-off bytes and the CRC check value;
- the usable range;
- the golden transcript;
- the acknowledgement of turn-off;
- the station config.
