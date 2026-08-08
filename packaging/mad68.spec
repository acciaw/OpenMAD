# PyInstaller spec for OpenMAD, the MAD68 HE driver.
#
# Produces one windowed executable that starts the tray. The HUD is served from
# the same process, so there is no second binary and no browser extension --
# "Open" on the tray menu points a browser at the local server.
#
#     pip install pyinstaller
#     pyinstaller packaging/mad68.spec
#
# Output: dist/OpenMAD.exe
#
# `console=False` matters: with a console the app opens a terminal window on
# every launch, and pythonw-style startup entries would flash one on login.

import configparser
from pathlib import Path

REPO = Path(SPECPATH).parent

# Same version.ini the app and the installer read.
_ini = configparser.ConfigParser()
_ini.read(REPO / "version.ini", encoding="utf-8")
APP_NAME = _ini.get("app", "name", fallback="OpenMAD")

a = Analysis(
    [str(REPO / "tools" / "tray.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    # Deliberately no profiles/ and no switcher.json. The repository root is the
    # working data directory during development, so those files are whoever's
    # machine built the release -- bundling them shipped the developer's own
    # profiles and app rules to every user. A fresh install creates a plain
    # "Default" profile by itself (profile.ensure_default_profile), and
    # mad68.paths keeps user data in %APPDATA% from then on.
    datas=[
        # Identity is read from this at runtime, so it has to travel
        # with the executable.
        (str(REPO / "version.ini"), "."),
        # Optional branding. PyInstaller errors on a missing datas path,
        # so only include it once the folder has something in it.
        *([(str(REPO / "assets"), "assets")]
          if (REPO / "assets").exists() else []),
    ],
    hiddenimports=[
        # pystray and PIL pick their backend at runtime, so the analyser cannot
        # see these imports.
        "pystray._win32",
        "PIL._tkinter_finder",
    ],
    excludes=[
        # Nothing in the app draws plots or parses XML; excluding these keeps
        # the binary from doubling in size.
        "matplotlib", "numpy", "scipy", "pandas", "tkinter.test",
        "test", "unittest", "pydoc_data",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=str(REPO / "assets" / "icon.ico")
    if (REPO / "assets" / "icon.ico").exists() else None,
)
