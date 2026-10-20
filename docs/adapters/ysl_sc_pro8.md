# YSL Photonics SC-PRO8 supercontinuum laser (`ysl_sc_pro8`)

`gmcore/devices/ysl_sc_pro8.py`. Emission, power and repetition rate, plus the
laser's state and alarm.

**Status:** written from the vendor's Python driver (`YSL-SC-PRO8/SC_PRO8.py`,
`SerialPort.py`).

- **Checked here:** every command is byte-identical to the vendor code's (run
  against a stand-in serial port).
- **Not verified on hardware:** the exact replies. They are taken from what the
  vendor code compares against.

## Station config

```toml
[devices.SC]
driver = "ysl_sc_pro8"
resource = "COM7"                    # or "ASRL7::INSTR"; 9600 8N1
max_power = 20.0                     # % cap (default 100; a new entry starts at 20)
```

The server's driver menu lists it under **YSL Photonics**.

## Commands

ASCII, ending in CR. Replies are CR-separated lines, some starting with a CR.
The transport collects them until the port has been quiet for 0.15 s.

| Purpose | Command | Reply |
| --- | --- | --- |
| emission on | `LA1` | `OK` `OK` |
| emission off | `LA0` | `OK` |
| power | `Cu<nnn>`: nnn = % · 6, `000`-`600` | `OK` |
| repetition rate | `fr0<nnnn>0`: nnnn = MHz · 100 | `OK` |
| state | `STATE?` | `FAULT`, `READY`, `Warm Up`, `Laser On` |
| alarm | `Er?` | `Err00` = no alarm; `Err01`…, `ERR11`… |

A setting is accepted only when every reply line is `OK`. Otherwise the error
names the reply and adds the state and alarm, e.g.
`'LA1' answered ['ERR'] (state FAULT, alarm Err05)`.

- **Power:** resolution 1/6 %; the value is rounded to the nearest step.
- **Repetition rate:** 0.01, 0.1, 0.5, 1, 4, 5, 10, 20, 40 or 80 MHz, the
  vendor's table. Its docstring also lists 0.2 MHz, which the table lacks, so
  0.2 MHz is refused.

## Readings

- **Queried each poll (`STATE?`):**
  - `<name>.emission`: 1 while `Laser On`, shown as a status light;
  - `<name>.fault`: 1 on `FAULT`, shown red.
- **Software level** (the laser has no query for them): `<name>.power` (%) and
  `<name>.rate` (MHz) read what the driver set. They are NaN after connect.
- **Status group (level 1):** State and Alarm, read on connect and on
  **Update settings**.

## Safety

- **Emission ON** asks for confirmation.
- The driver also refuses it until a **power has been set in this session**.
  The laser's present power can't be read, so it never starts at a current
  nobody chose.
- **Safe state** = emission off (`LA0`). The power setting is kept.

## Tests

`tests/test_ysl.py`:

- the golden transcript;
- the 1/6 % rounding;
- the refusals (power cap, unknown rate, emission before power, a faulted
  laser);
- the transport;
- the station config.
