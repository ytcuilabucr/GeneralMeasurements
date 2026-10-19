# Control settings checked against the manuals (2026-10-18)

Every control the drivers and panels expose was compared with the
manufacturer's documentation. Instrument figures now live in the driver
(checked when it is built, so `gm validate` catches them without hardware) and
in the definition (shown by the GUI). The commands are unchanged unless listed.
Nothing here has been run on hardware yet beyond what the adapter pages say.

## Sources

| Instrument | Document |
| --- | --- |
| Keithley 2400, 2401, 2410 | Series 2400 datasheet (1KW-2798-1); Model 2401 datasheet; Series 2400 User's Manual 2400S-900-01 Rev. K (download.tek.com) |
| Keithley 2450 | SPEC-2450 Rev. B (Nov 2017); Reference Manual 2450-901-01 Rev. D (May 2015) |
| Keithley 2000 | User's Manual 2000-900-01 Rev. J (Aug 2010) |
| SR830, SR850 | Stanford Research Systems operating manuals (thinksrs.com) |
| Lake Shore 335 | Model 335 User's Manual |
| Lake Shore 625 | Model 625 User's Manual, P/N 119-037 Rev. 1.8 (Dec 2017) |
| LM-510 | Cryomagnetics LM-510 Operating Instruction Manual, version 1.0 |
| Giga-tronics 2420B | 2400 Series datasheet (2420L/M). **No 2420B manual found**; see below |

## Findings and changes

### Keithley 2400 / 2401 / 2410 / 2450

Split into one driver per model, with each model's ranges and limits from the
documents above. The full table is in
[adapters/keithley_smu.md](adapters/keithley_smu.md).

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Model maximum | 2401: ±20 V | ±21 V @ ±1.05 A | ±21 V |
| Compliance | not checked | 2400 family minimum 0.1 % of range; limited by the power envelope (above ±21 V: 105 mA, 21 mA on the 2410) | checked at construction |
| Source range | not available | per-model lists; level ≤ 105 % of range | drop-down (level 1), output-off only, level and limits must fit |
| Measure range | not available | per-model lists; below the compliance it clamps the output (2400 family) or lowers the limit (2450) | drop-down (level 1); effective compliance shown; 2450 limit re-read |
| NPLC | not checked | 0.01 … 10 | checked |
| Units in curr mode with the default compliance | 1 µV accepted | below 0.1 % of the lowest range | refused |

### Keithley 2000

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Range | free number (the meter picked the next range up) | DCV 100 mV … 1000 V; DCI 10 mA, 100 mA, 1 A, 3 A; Ω 100 Ω … 100 MΩ | drop-down of the present function's ranges + Auto; others refused |
| NPLC | 0.1, 1, 10 | 0.01 … 10 | adds 0.01; station value checked |

### SR830 / SR850

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Sensitivity | 27 steps, 2 nV … 1 V | SENS 0 … 26 = 2 nV … 1 V (both models) | unchanged (matches) |
| Time constant | 20 steps, 10 µs … 30 ks | OFLT 0 … 19 = 10 µs … 30 ks; > 30 s refused above 200 Hz; short ones raised to the filter minimum | read back after setting; a refused/adjusted value raises and shows what the unit took |
| Amplitude (station) | not checked | SLVL 0.004 … 5.000 V rms | checked |
| Frequency (station) | not checked | FREQ 0.001 … 102 000 Hz, internal reference only | checked |

### Lake Shore 335

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Ramp rate | 0.1 … 100 K/min | RAMP 0.1 … 100 K/min | unchanged (matches) |
| Heater range | 0 … 3 on either loop | outputs 1 and 2 in current mode: Off/Low/Med/High; output 2 in **voltage** mode: 0 = off, 1 = on | loop 2 reads `HTRSET? 2`; in voltage mode only 0/1 are allowed |
| Setpoint | station limits | in the control sensor's units, no fixed span | unchanged |

### Lake Shore 625

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Current | station limits vs the supply's LIMIT? at configure | SETI/LIMIT up to ±60.1 A | also checked at construction against ±60.1 A |
| Ramp rate | > 0 | RATE 0.0001 … 99.999 A/s, 4 decimals | checked at construction; a ramp below 0.0001 A/s (which would be sent as 0) is refused |
| Switch heater, limits | never written | — | unchanged (operator-owned) |

### Cryomagnetics LM-510

| Control | Before | Manual | Now |
| --- | --- | --- | --- |
| Interval | 1 s … 99:59:59 | INTVL 00:00:00 … 99:59:59; 0 = sample continuously | unchanged; 0 stays refused (continuous boil-off), now said in the tooltip. The panel's minutes box went from 24 h to 99:59 h |
| Mode | Sample/Hold, Continuous | MODE S or C (Off only from the front panel or an error) | unchanged (matches) |

### Giga-tronics 2420B

| Control | Before | Source | Now |
| --- | --- | --- | --- |
| Frequency | station limits | 2420: 10 MHz … 20 GHz (2420L/M datasheet) | checked in the driver and shown as the property's bounds |
| Power | capped at `max_power_dbm` | minimum −20 dBm (−110 dBm with the step attenuator); maximum leveled power depends on frequency | unchanged: the cap stays the station's, and a level the unit refuses is reported through `SYST:ERR?` |

The 2420B command syntax and its power limits still need its own manual. They
can also be checked on the instrument with RF off.

## Not covered

- The commands' behaviour on hardware (the range commands, `HTRSET?`, the
  time-constant read-back): run them on a dummy load, or with the outputs off,
  before relying on them.
- Settings the drivers don't expose (filters, 4-wire sense, triggering,
  lock-in reserve and slope, LS335 PID and zones, LS625 compliance voltage).
