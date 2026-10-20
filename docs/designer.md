# Window layouts in Qt Designer

Every window's fixed layout is a Qt Designer form (`.ui`), loaded when the
window opens. Edit a form in Designer, save, and restart the program: there is
no build step.

```
pyside6-designer gmcore/gui/forms/server.ui
```

`pyside6-designer` comes with PySide6 (the `gui` extra).

## Where the forms are

All forms are in one folder, `gmcore/gui/forms/`; the code paths below are
under `gmcore/gui/`:

| Form | Window | Code |
| --- | --- | --- |
| `forms/panel.ui` | the standard instrument panel (Keithleys, DMM, RF, lock-ins) | `common/panels.py` (`Panel`, `DefinitionPanel`) |
| `forms/lakeshore_335.ui` | temperature controller panel | `common/dedicated_panels/lakeshore_335.py` |
| `forms/lakeshore_625.ui` | magnet supply panel | `common/dedicated_panels/lakeshore_625.py` |
| `forms/lm510.ui` | helium level panel | `common/dedicated_panels/lm510.py` |
| `forms/server.ui` | Instrument Server main window: Config and Quick view tabs | `server/window.py` (`InstrumentServer`), `server/quick.py` |
| `forms/panel_window.ui` | the window around one panel | `server/window.py` (`PanelWindow`) |
| `forms/general_measurement.ui` | General Measurement | `measurement/window.py` |
| `forms/view_window.ui`, `field_picker.ui` | monitor / chart window, field picker | `legacy/views.py` |

The code loads a form by its file name (`load_ui("server", self)`); a test
checks that every form loads and is used.

## What the form holds, and what the code adds

The form holds everything fixed:
- menus and actions;
- group boxes, labels, buttons and their texts, tooltips and styles;
- the cryostat panels' cards, lights and spin boxes.

The code fills what depends on the config or the driver:
- the server's instrument rows, added under the form's header row;
- the standard panel's readings and settings rows, built from the driver definition (`devices/defs/`);
- values and units read from the instrument.

## Rules

- **Names are the contract.** The code reaches widgets by their object name (`btn_connect`, `cardA_value`, `actionSave`). Renaming one in Designer breaks the code that uses it, and `tests/test_ui.py` checks that every named widget loads. Rename freely the widgets the code doesn't use, such as labels and spacers.
- **Every panel form has the same header and footer names:** `title`, `status`, `btn_connect`, `btn_disconnect`, `info`, `live`, `interval`, `btn_read`, `btn_safe`. The standard panel also has `readings` (grid), `ctrl_box` and `form` (the Control box), `btn_update` (Update settings; the cryostat forms have one too), `main_split` and `charts_split` (the splitters; the code adds the folded charts to `charts_split`).
- **Names that would hide a method, or data the code set before loading (`charts`, `rows`), are refused at load.** For example, `close`, `move`, `scroll` or `update` would replace Qt or panel methods, and the panel would break in confusing ways. The loader stops with "would replace a method; rename it in Designer".
- **Cards** (the big-number boxes on the cryostat panels) are plain `QFrame`s with the dynamic property `card = true`. Each holds labels named `<frame>_caption`, `<frame>_value` and `<frame>_sub`, and anything else you like. Style them with the form's style sheet (`QFrame[card="true"]`).
- **The accent colour** of a cryostat panel is in its form's top-level style sheet. Change it there and in the module's `ACCENT` constant (used for drawing).

## Our own widgets in Designer

Designer can't draw our Python widget classes, so they are "promoted" widgets.
Designer shows the base class, and the program creates the real one. Set their
options as dynamic properties (Property Editor, the **+** button, String):

| Class (promoted from) | Properties |
| --- | --- |
| `Led` (QLabel) | `label`: the text next to the light |
| `TimeChart` (QWidget) | `yLabel`, `yUnit`, `curves_spec` (`"A=#e67e22; B=#5dade2"`) |
| `SIEdit` (QLineEdit) | `unit`: the hint shown when empty |
| `CsvLog`, `DewarGauge`, `FieldBar` (QWidget) | none; the code sets them |
| `PlotWidget` (QGraphicsView, header `pyqtgraph`) | none |

To add one: place its base class, right-click, **Promote to…**. Enter the class
name and the header (`gmcore.gui.common.widgets`, or
`gmcore.gui.common.panels` for `SIEdit`).

## Icons

Icons are [Material Design Icons](https://pictogrammers.com/library/mdi/) (MDI 6),
drawn by QtAwesome (part of the `gui` extra). They are not set in Designer; the
loader adds them by object name (`gui/common/icons.py`):

- `ICONS`: role → MDI name (`"connect": "mdi6.lan-connect"`). Change an icon
  here and every window follows. Browse the names with `qta-browser`.
- `BY_NAME`: form object name → role. Any widget or action named `btn_connect`,
  `actionSave`, ... gets that role's icon when its form loads, in every form; tab
  pages too (`config_tab`, `quick_tab`).
- `ACTIONS`: a definition's action id → role (Configure → restore, Auto phase →
  sine wave; others a play icon). `DRIVERS`: driver → window icon.
- Buttons the code builds (setting rows, server rows, Auto zoom) use the same
  roles through `icons.icon(role)` / `icons.set_icon(button, role)`.

To give a new button an icon: name it in Designer, add the name to `BY_NAME`.
A test checks every role resolves and every name in `BY_NAME` exists in a form.
Without QtAwesome installed, the windows simply have no icons.

Icons without a colour of their own (`COLORS`) take the palette's text colour,
so they show in both themes. Set them through `set_icon`, `set_window_icon` or
`set_tab_icon`, not `setIcon(icon(...))`: those helpers remember the role, so
`icons.refresh()` can redraw the icon when the theme changes.

## Theme

An application's main window (the Instrument Server; the legacy station and
bench) gets a **Miscellaneous** menu (`menuMiscellaneous`) with Follow system /
Bright / Dark by calling `theme.add_menu(self)`; instrument panel windows have
none and simply follow the setting. Don't add it in Designer.
Colours written into a style sheet or painted in code must read in both
themes: use `palette(...)` roles, or set both the background and the text.

## Loading time

Measured on the lab PC, per window:
- simple windows (server, panel window, dialogs): 5 to 7 ms;
- standard panel: about 19 ms;
- cryostat panels: about 30 ms.

Most of that is creating the widgets, mainly the charts, which costs the same
when a window is built in code. Each form loads once, when its window opens.
