"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True, scope="session")
def _misc_settings(tmp_path_factory):
    """The GUI's own settings (theme, last config, ...) go to a scratch file, never
    the real `Instrument config/misc.toml`."""
    try:
        from gmcore.gui.common import appsettings
    except ImportError:                     # no GUI installed: nothing to redirect
        yield
        return
    appsettings.use(appsettings.MiscSettings(tmp_path_factory.mktemp("misc") / "misc.toml"))
    yield
    appsettings.use(None)
