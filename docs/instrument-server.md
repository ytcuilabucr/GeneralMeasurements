# Instrument Server

The Instrument Server connects to the instruments of an instrument config and
owns their sessions. It is one of the two main GUIs; the other, General
Measurement (sequences), is not built yet. See the project plan, "Applications".

```
python instrumentserver.py ["Instrument config/lab.toml"]
gm server ["Instrument config/lab.toml"]
```

Without an argument it opens `Instrument config/lab.toml` if it exists.

## Instrument config

The files in `Instrument config/` use the station schema
([station config](../README.md)): a `[station]` table and one
`[devices."<name>"]` table per instrument, with its driver, address and
settings. `gm validate`, `gm dry-run` and `gm run --station` accept them too.

- `lab.toml`: the lab GPIB bus.
- `sim.toml`: simulated instruments, for trying the server without hardware.
- `example.toml`: a template to copy.

These are yours to edit, and so is saving from the server. The tests use
their own copies in `tests/fixtures/`.

The name is the instrument's **working name**: sequences and data keys use it
(`Keithley 2400 - 1.v`). It must be unique and can't contain `.`, `/`, `\` or
quotes, or start or end with a space.

## The table

One row per instrument in the config:

| Column | |
| --- | --- |
| Address | the VISA resource (`simulated` for simulators) |
| Name | the working name; edit to rename. Clearing it gives the default, the model and an index (`Keithley 2400 - 1`) |
| Driver | the driver, picked from a two-level menu: the maker (Keithley, Lake Shore, Stanford Research, Giga-tronics, Cryomagnetics, Simulated), then the model; the present one is ticked. A different driver starts from built-in default settings; going back restores the file's settings |
| Connection | grey: disconnected; blue: connecting; green: connected; amber: connected but the last read failed; red: the connection failed (tooltip: the error) |
| Connect / Disconnect | green **Connect** opens the session and reads the present state (no reset, no writes); red **Disconnect** closes it, leaving the instrument as it is |
| Open panel | the instrument's panel in its own window: the dedicated panel when the driver has one (temperature, magnet, helium level), else the standard panel from the driver's definition, else a generic one |

- **While connected, the name and driver are fixed.** Disconnect to change them.
- **Panel windows:** closing a panel window only hides it, and the connection stays up. Opening a panel doesn't connect; use the panel's own **Connect** or the table's.
- **Errors:** a row whose settings don't validate can't connect, and the tooltip says why.
- **Add instrument** is not available yet; edit the config file for now.

## File menu

**Open…**, **Save** and **Save as…** read and write the config.
- **Saving keeps the file's comments and layout** for every instrument whose driver didn't change. A renamed instrument keeps its comments, and its name in `shutdown_order` follows the rename. An instrument with a new driver is written fresh.
- **Unsaved changes** show as `*` in the title, and opening another file or closing asks first.
- **Another config can't be opened while anything is connected** (single owner).

Settings changed in a panel, like the Keithley's **Switch mode…**, are not saved to the config.

## Window menu

**General Measurement (layout preview)** opens the sequence editor's planned
layout, listing the server's instruments (and their fields, once connected).
Nothing in it runs yet.

## Closing

Closing the server with instruments connected asks **Leave as is** or **Safe
state, then close** (`safe_state()` on each, as in a scan's shutdown).

## Code

`gmcore/gui/server/`: `window.py` (`InstrumentServer`,
`InstrumentRow`, `PanelWindow`) and the forms `gui/forms/server.ui` and
`gui/forms/panel_window.ui` ([designer.md](designer.md)). The panels come from
`gui/common/`, shared with General Measurement. Tests: `tests/test_server.py`.
