"""Panels with their own look, for the cryostat instruments. One module per
instrument, named like its driver in devices/ (the drivers stay Qt-free)."""

from .lakeshore_335 import LakeShore335Panel
from .lakeshore_625 import LakeShore625Panel
from .lm510 import LM510Panel

CRYO_PANELS = {
    "lakeshore_335": LakeShore335Panel,
    "lakeshore_625": LakeShore625Panel,
    "lm510": LM510Panel,
}

__all__ = ["CRYO_PANELS", "LM510Panel", "LakeShore335Panel", "LakeShore625Panel"]
