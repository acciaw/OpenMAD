"""Reads version.ini, the one place the app's identity is defined.

The installer script reads the same file with the Inno preprocessor's ReadIni,
so there is nothing to keep in sync by hand.

Values are read once at import. Frozen builds bundle version.ini next to the
executable; from a source checkout it sits at the repository root.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

# Fallbacks used only if version.ini is missing, which should not happen in a
# packaged build but should not crash the app either.
_DEFAULTS = {
    "name": "OpenMAD",
    "version": "0.0.0",
    "repo": "",
    "publisher": "",
    "url": "",
}


def _locate() -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller unpacks bundled data to _MEIPASS, and the exe's own
        # directory is checked too so the file can be edited beside a build.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "version.ini")
        candidates.append(Path(sys.executable).parent / "version.ini")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "version.ini")
    for path in candidates:
        if path.exists():
            return path
    return None


def _load() -> dict:
    path = _locate()
    if path is None:
        return dict(_DEFAULTS)
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        section = parser["app"]
    except Exception:
        return dict(_DEFAULTS)
    return {k: section.get(k, v) for k, v in _DEFAULTS.items()}


_app = _load()

APP_NAME: str = _app["name"]
APP_VERSION: str = _app["version"]
GITHUB_REPO: str = _app["repo"]
PUBLISHER: str = _app["publisher"]
PROJECT_URL: str = _app["url"]
