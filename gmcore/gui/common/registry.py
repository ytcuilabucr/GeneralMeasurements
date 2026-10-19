"""Driver name -> panel class, for every panel in the GUI."""

from ...config.registry import DRIVER_CLASSES
from .dedicated_panels import CRYO_PANELS
from .panels import PANELS as _GENERAL
from .panels import DefinitionPanel, GenericPanel, Panel

PANELS: dict[str, type[Panel]] = {**_GENERAL, **CRYO_PANELS}


def panel_for(driver: str) -> type[Panel]:
    """The driver's dedicated panel if it has one (common/dedicated_panels/), else the
    standard panel when the driver has a definition, else the generic one."""
    if driver in CRYO_PANELS:
        return CRYO_PANELS[driver]
    if getattr(DRIVER_CLASSES.get(driver), "DEFINITION", None):
        return DefinitionPanel
    return PANELS.get(driver, GenericPanel)
