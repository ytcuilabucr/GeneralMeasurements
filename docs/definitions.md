# Driver definitions

Each driver's properties are declared in a TOML file in
`gmcore/devices/defs/`. The file says **what** the properties
are: kind, units, limits, who reads and who writes them, and what asks for a
confirmation. The driver's Python says **how**: `get`, `set`, `attach` and so
on name its methods or attributes. The design follows Labber's instrument
`.ini` files, but avoids that format's known weak points (numbered keys,
parallel combo lists, implicit rate units, one gating rule per property).

What is built from a definition:

- `describe()`, which scans record and monitor/chart windows offer. Its keys
  and units are exactly what the hand-written versions produced.
- The GUI's standard panel (`DefinitionPanel`), used for the Keithleys, the
  RF source and the lock-ins. The cryostat panels keep their own layouts, but
  their drivers have definitions too, and `DefinitionPanel` can show them.

Drivers stay free of Qt, and every write still goes through the driver, which
enforces the limits and rates.

## The rule for modes

A setting is a **mode** when it changes the units or limits of other
properties, or which property is set and which reads it back. Each mode gets
its own file:

| Driver | Mode (station key) | Files |
| --- | --- | --- |
| `keithley_2400`, `keithley_2401`, `keithley_2410`, `keithley_2450` | `mode`: `volt`, `curr` | per model: `keithley_<model>.toml` + `keithley_<model>_volt.toml`, `_curr.toml` |
| `keithley_2000` | `function`: `volt`, `curr`, `res`, `fres` | `keithley_2000.toml` + one file per function |
| `gigatronics_2420b` | `axis`: `freq`, `power` | `gigatronics_2420b.toml` + `_freq.toml`, `_power.toml` |
| `lakeshore_335`, `lakeshore_625`, `lm510`, `sr830` / `sr850` | none | one file each (the lock-ins share `srs_lockin.toml`) |

- The main file lists `modes` and `mode_attr`. A mode file has
  `mode = "<mode>"`. Its `[properties.X]` tables add to the main file's
  properties or override them field by field, and new properties go last.
- A unit conversion is not a mode. The 625's A or T axis uses templates
  (`unit = "{units}"`) instead.
- A connected device's mode is fixed. To change it, the driver's `switch`
  method does the instrument side, and the panel then reloads the definition.
  For example, a Keithley SMU ramps to 0, turns the output OFF, then runs
  `*RST` and configures the new function.
- If the new mode needs station settings (the Keithley needs compliance,
  limits and max rate in the new units), they come from the station file:

  ```toml
  [devices.bias.modes.volt]        # lets the GUI switch bias to voltage
  compliance = 1e-6                # A
  limits = [-1.0, 1.0]             # V
  max_rate = 0.1                   # V/s
  ```

  Without that table, Switch mode… refuses and says what to add. A switch
  lasts until the GUI closes. The station file is not changed.

## File layout

```toml
drivers = ["keithley_2410"]        # the driver this file serves (one model, one file)
label = "Keithley 2410 SMU"        # panel title (template)
info = "..."                       # one line under the title (template)
mode_attr = "mode"                 # driver attribute and station key holding the mode
modes = { volt = "Voltage source", curr = "Current source" }
attach = "sync"                    # run on connect, queries only
check_mode = "active_mode"         # instrument's present mode; mismatch disables settings
switch = "switch_mode"             # switch(mode, **settings)
switch_confirm = "..."             # {mode}, {mode_label} available
switch_settings = ["compliance", "limits"]   # asked for in a dialog when switching
switch_interlock = "!output"       # Switch mode… only while this is false / zero
read_if = "output"                 # poll read() only while this bool property is true
safe = "Level 0 (one step), then output OFF"   # what safe_state() does
# in a mode file: the dialog's pre-filled values when switching into that mode,
# unless the station has [devices.X.modes.<mode>]
# switch_defaults = { compliance = 1e-6, limits = [-1.0, 1.0] }
groups = ["Source", "Measure"]     # display order of the property groups

[properties.level]                 # the id: snake_case, stable
...
```

## Property keys

| Key | Meaning |
| --- | --- |
| `kind` | `float` (default), `int`, `bool`, `enum`, `str`, `status` (a 0/1 light), `action` (a button, no value) |
| `access` | `read` (default), `write` or `both` |
| `level` | how often it is used: `0` essential (always on the panel), `1` expanded, `2` all (see [Levels](#levels)) |
| `label`, `unit`, `tooltip`, `group` | display; `label` and `unit` are templates |
| `key` | `read()` key suffix: the default is the id, and `""` means the device name itself (`temp`, `dmm`) |
| `poll` | `live` = the value comes from `read()` (the default when there is no `get`); `connect` = from `get`, read on connect and after every write |
| `get` | an attribute (`nplc`), a method (`get_heater_range`), or `method.key` to take one key of a returned dict (`snapshot.pid`; called once per refresh) |
| `set` | a method taking the value; for an action, a method with no argument |
| `choices` | enum: `{label = value}`, one map, so labels and wire values can't drift apart |
| `choices_attr` | enum: a driver attribute holding a list of values instead (lock-in sensitivity table) |
| `allow_auto` | `None` is valid, shown as "auto"; an enum's drop-down then starts with Auto (DMM and SMU ranges) |
| `limits`, `min`, `max` | numbers, or the name of a driver attribute (`limits = "limits"`, `max = "max_power_dbm"`); shown on the panel, enforced by the driver |
| `readback` | the property whose reading confirms this setting: the Keithley level reads back as `v` or `i`, depending on the mode |
| `tolerance` | the difference from the readback that counts as reached (number or attribute) |
| `ramp` | `{ max_rate, unit, start, stop }`: the rate's unit is explicit (`"V/s"`, `"K/s"`). `start(target, rate)` returns at once; without it, `set()` ramps itself. `stop` holds a running ramp |
| `stoppable` | a long `set()` that Stop can cancel |
| `confirm`, `confirm_when` | ask before writing, for every value or only for the listed ones (`[true]`, `["Low", "Med", "High"]`). In the text, `{value}` and `{<property id>}` give values as displayed |
| `interlock` | a bool property that must be true (`"x"`) or false (`"!x"`) before this one can be written |
| `when` | a driver attribute: the property applies only when it is true (`"check_compliance"`) or false (`"!x"`) |
| `severity` | status: `amber` (default) or `red` when true |
| `expose` | live properties: `false` keeps the field out of `describe()`, so it isn't recorded or offered to views |

Templates fill `{name}` and the driver's attributes, with attribute and index
access and format specs: `"{d.model}"`, `"{limits[0]:g}"`.

## Levels

Every property says how often it is used, so the panel needn't show
everything at once:

| Level | Name | What goes there | Examples |
| --- | --- | --- | --- |
| 0 | Essential | what you watch or change in every session, and anything safety-relevant | SMU output, level and compliance light; lock-in X, Y, sensitivity, time constant; setpoint, heater range |
| 1 | Expanded | changed now and then, or worth a look | SMU compliance and Configure; DMM NPLC and range; lock-in R, phase, reference frequency, Auto phase; magnet switch heater, errors |
| 2 | All | rarely used, or read-only details | SMU NPLC; lock-in identity; Lake Shore PID; magnet supply limits; LM-510 high mark, sensor length |

The standard panel always shows level 0. Level 1 is folded into a **More**
toggle list, and level 2 into an **All** toggle inside it. Each level keeps
its own readings, group headings and actions. Levels only change what is
shown: every live property is still read, recorded and offered to the
monitor and chart windows.

Write `level` on every property, in the file that introduces it (a mode
file only for the properties it adds). The model's default is 0, and
`test_every_property_declares_its_level` checks the shipped files.

## Checks

Loading a definition validates it with pydantic:

- unknown keys;
- ids that aren't snake_case;
- an action without `set`;
- a writable property without `set`;
- an enum without choices;
- a `readback` or `interlock` naming no property;
- `confirm_when` values that aren't among the choices;
- duplicate `read()` keys;
- a `level` other than 0, 1 or 2; an `interlock` at a higher level than the
  property it locks (it would be folded away while the property is shown);
  a `read_if` property above level 0 (it says why the readings stop).

`tests/test_definitions.py` checks that every method, attribute and template
field a definition names exists on its driver, in every mode, and that
reading the `get` properties only queries the instrument.

## Adding an instrument

1. Write the driver (see [adding-an-adapter.md](adding-an-adapter.md)), with
   `DEFINITION = "<file name>"` and `describe()` returning
   `definitions.describe(self)`.
2. Write `defs/<driver>.toml`, plus one file per mode if it has modes.
3. Register it: its config class in `config/schema.py` (`DeviceCfg`, and
   safe `DRIVER_DEFAULTS` if it moves anything), its builder and
   `DRIVER_CLASSES` entry in `config/registry.py`, and its model name in
   `MODEL_NAMES` (`gui/server/window.py`) for default names.
4. The Instrument Server then shows it with the standard panel. For a panel of
   its own, see [designer.md](designer.md) and `gui/common/dedicated_panels/`.
