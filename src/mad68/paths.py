"""Where the app reads what ships with it, and where it writes what the user owns.

These are two different places, and treating them as one is what broke the
packaged build.

Bundled resources -- the icons, the starter profiles, version.ini -- are
read-only and travel inside the executable. PyInstaller unpacks them to a
temporary directory that is deleted the moment the process exits.

The user's data -- switcher.json, settings.json, onboard.json, their profiles,
their backups -- has to outlive the process. Writing it beside the resources
meant every change was thrown away on exit, which is why nothing survived a
reboot and the HUD had to be reopened to set everything up again.

From a source checkout both are the repository root, so development is unchanged
and the repo stays the working tree it has always been.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .version import APP_NAME

_REPO = Path(__file__).resolve().parent.parent.parent


def is_frozen() -> bool:
    """True in a PyInstaller build, false from a source checkout."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Read-only files that ship with the app.

    _MEIPASS is where a one-file build unpacks its bundle. The executable's own
    directory is the fallback for a one-folder build, and for editing a file
    beside a build without rebuilding.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return _REPO


def data_dir() -> Path:
    """Writable files belonging to the user, which must survive a restart.

    %APPDATA% rather than beside the executable: the app installs per-user into
    Program Files, which is not writable without elevation, and the whole point
    of the per-user install is that it never asks for any.
    """
    if not is_frozen():
        return _REPO
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / APP_NAME


def profile_dir() -> Path:
    return data_dir() / "profiles"


def assets_dir() -> Path:
    """Branding. Bundled, so it is a resource and never follows the data dir."""
    return resource_dir() / "assets"


def seed_user_data() -> Path:
    """Fill in anything the user does not have yet, and return the profile dir.

    Only ever fills gaps: an existing file is never overwritten, so a profile the
    user has edited survives an upgrade that happens to ship a newer copy under
    the same name.
    """
    dest = profile_dir()
    dest.mkdir(parents=True, exist_ok=True)
    if not is_frozen():
        return dest
    src = resource_dir() / "profiles"
    if src.is_dir():
        for f in src.glob("*.json"):
            target = dest / f.name
            if not target.exists():
                try:
                    shutil.copy2(f, target)
                except OSError:
                    pass  # a starter profile failing to copy must not stop startup
    return dest
