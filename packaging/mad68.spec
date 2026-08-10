# PyInstaller spec for OpenMAD, the MAD68 HE driver.
#
# Produces one windowed executable that starts the tray. The HUD is served from
# the same process, so there is no second binary and no browser extension --
# "Open" on the tray menu points a browser at the local server.
#
#     pip install pyinstaller
#     pyinstaller packaging/mad68.spec
#
# Output: dist/OpenMAD/ -- OpenMAD.exe plus an _internal folder beside it.
#
# One *folder*, not one file, and that is deliberate.
#
# A one-file build appends the whole bundle to the executable and unpacks it
# into %TEMP%\_MEInnnnnn on every single launch, then deletes it on exit. That
# cost an update: the installer started the new 19 MB build six milliseconds
# after finishing writing it, the unpack did not complete, and the app died on
#
#     Failed to load Python DLL '...\_MEI286442\python313.dll'.
#     LoadLibrary: The specified module could not be found.
#
# while the same executable started by hand moments later worked every time.
# %TEMP% was also left holding orphaned _MEI directories from earlier partial
# unpacks. A one-folder build has nothing to unpack -- python313.dll simply
# sits next to the executable -- so that entire class of failure cannot happen,
# startup is markedly faster, and nothing is written to %TEMP% at all.
#
# The single-file form only ever bought portability, and this app ships through
# an installer, so it bought nothing.
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

# exclude_binaries=True is what makes this a one-folder build: the executable
# becomes a small launcher and COLLECT below places everything else beside it,
# rather than the binaries and data being appended to the exe for unpacking at
# runtime.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(REPO / "assets" / "icon.ico")
    if (REPO / "assets" / "icon.ico").exists() else None,
)

# Everything the exe needs, in dist/OpenMAD/. PyInstaller puts the payload in
# an _internal subfolder and points sys._MEIPASS at it, which is exactly what
# mad68.paths.resource_dir() already reads -- so bundled assets and version.ini
# are found the same way they were in the one-file build, with no app change.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
