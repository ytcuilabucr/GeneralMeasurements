"""Instrument Server: connect to the instruments of an instrument config.

    python instrumentserver.py ["Instrument config/lab.toml"]

The same as `gm server`, runnable without installing the package. See
gmcore/gui/server/ and docs/instrument-server.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))      # run without installing

import gmcore.gui  # noqa: E402, F401  (pins pyqtgraph to PySide6)
from gmcore.gui.server.window import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
