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

## Tabs

- **Config**: the instrument table below, the **Add instrument** button and the
  log.
- **Quick view**: one block per connected instrument, stacked vertically, with
  only its readings (big numbers, labels without the instrument name).
  - Blocks appear when an instrument connects and go when it disconnects.
  - The values come from the readings each panel already polls (at its Live
    interval), so the quick view adds no traffic to the instruments.
  - **Settings…** opens a checkbox for every reading of every connected
    instrument, grouped by instrument (an instrument's box ticks or clears
    all of its readings). Untick a reading to hide it.
  - The hidden readings are remembered in `Instrument config/misc.toml`
    (see below), so they stay hidden after a reconnect
    or a restart. An instrument whose readings are all hidden gets no block.

## The table

One row per instrument in the config:

| Column | |
| --- | --- |
| Address | the VISA resource (`simulated` for simulators) |
| Name | the working name; edit to rename. Clearing it gives the default, the model and an index (`Keithley 2400 - 1`) |
| Driver | the driver, picked from a two-level menu: the maker (Keithley, Lake Shore, Stanford Research, Giga-tronics, Cryomagnetics, Simulated), then the model; the present one is ticked. A different driver starts from built-in default settings; going back restores the file's settings |
| Connection | grey: disconnected; blue: connecting; green: connected; amber: connected but the last read failed; red: the connection failed (tooltip: the error) |
| Connect / Disconnect | green **Connect** opens the session and reads the present state (no reset, no writes); red **Disconnect** closes it, leaving the instrument as it is |
| Safe state | the instrument's `safe_state()` (as the panel's **Safe state**; the tooltip says what it does), after asking. Greyed out while disconnected |
| Open panel | the instrument's panel in its own window: the dedicated panel when the driver has one (temperature, magnet, helium level), else the standard panel from the driver's definition, else a generic one |

- **While connected, the name and driver are fixed.** Disconnect to change them.
- **Panel windows:** closing a panel window only hides it, and the connection stays up. Opening a panel doesn't connect; use the panel's own **Connect** or the table's.
- **Errors:** a row whose settings don't validate can't connect, and the tooltip says why.
- **Add instrument** is not available yet; edit the config file for now.

On the same row, at the right:

- **Connect all** connects every instrument that isn't connected and whose
  settings validate. A simulated lock-in connects once the source it measures is
  up.
- **Set all safe** calls `safe_state()` on every connected instrument, after
  asking: first in the config's `shutdown_order`, then the rest. One failure
  doesn't stop the others; the log lists any errors. The same sequence as a
  scan's shutdown.
- **Disconnect all** closes every session; the instruments keep their present
  state.

## File menu

**Open…**, **Save** and **Save as…** read and write the config.
- **Saving keeps the file's comments and layout** for every instrument whose driver didn't change. A renamed instrument keeps its comments, and its name in `shutdown_order` follows the rename. An instrument with a new driver is written fresh.
- **Unsaved changes** show as `*` in the title, and opening another file or closing asks first.
- **Another config can't be opened while anything is connected** (single owner).

Settings changed in a panel, like the Keithley's **Switch mode…**, are not saved to the config.

## Window menu

**General Measurement** opens the measurement editor, which builds and runs
measurements on the connected instruments
([general-measurement.md](general-measurement.md)). While a measurement runs,
the server can't be closed.

## Miscellaneous menu

Only the server window has this menu. **Follow system**, **Bright** or
**Dark** is one setting for the whole application: the panel windows and
General Measurement follow it (`gui/common/theme.py`).

**Open the last instrument config at start** (on by default): the server
starts with the config last opened or saved. With it off, or when that file is
gone, it starts with `Instrument config/lab.toml`. A file named on the command
line always wins.

## App settings file: `Instrument config/misc.toml`

The app's own settings are kept in one readable file, written whenever one
changes:

```toml
[appearance]
theme = "dark"                       # system / bright / dark

[instrument_server]
last_config = "Instrument config/lab.toml"
open_last = true

[quick_view]
hidden = ["Keithley 2400 - 1.i"]     # readings hidden in the Quick view
```

It can be edited by hand while the app is closed, and its comments are kept.
A missing file means the defaults; an unreadable one too, with a warning in the
log. It is per computer, so git ignores it. The instrument configs stay in
their own files: this one only names the last one used
(`gui/common/appsettings.py`).

## Closing

Closing the server with instruments connected asks **Leave as is** or **Safe
state, then close** (`safe_state()` on each, as in a scan's shutdown).

## Code

`gmcore/gui/server/`: `window.py` (`InstrumentServer`,
`InstrumentRow`, `PanelWindow`), `quick.py` (the Quick view and its Settings
dialog), and the forms `gui/forms/server.ui` and
`gui/forms/panel_window.ui` ([designer.md](designer.md)). The panels come from
`gui/common/`, shared with General Measurement. Tests: `tests/test_server.py`.
