"""Driver name -> panel class, for every panel in the GUI."""

from .instruments import CRYO_PANELS
from .panels import PANELS as _GENERAL
from .panels import GenericPanel, Panel

PANELS: dict[str, type[Panel]] = {**_GENERAL, **CRYO_PANELS}


def panel_for(driver: str) -> type[Panel]:
    return PANELS.get(driver, GenericPanel)
