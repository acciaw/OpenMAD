"""Application settings: start at sign-in, and update checking.

These are app-level preferences, not keyboard configuration, so they live apart
from profiles and from switcher.json. settings.json sits next to the profiles
directory.

Start-at-sign-in is the Windows per-user Run key rather than a scheduled task or
a service: it needs no elevation, it is what the installer's checkbox writes,
and the user can see and remove it from Task Manager's Startup tab.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Value name under HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
RUN_KEY_NAME = "OpenMAD"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass
class AppSettings:
    # Check GitHub for a newer release when the app starts.
    check_updates: bool = True
    # Mirrors the registry Run entry. The registry is the source of truth --
    # the user can remove the entry from Task Manager without us knowing, so
    # this is only a cache for display.
    start_at_login: bool = False

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            # No settings file yet: honour what the installer's wizard recorded,
            # otherwise the checkbox the user ticked during setup would be
            # ignored the first time the app runs.
            return cls(check_updates=_installer_check_updates(),
                       start_at_login=get_start_at_login())
        return cls(
            check_updates=bool(d.get("check_updates", True)),
            start_at_login=bool(d.get("start_at_login", False)),
        )

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps({"check_updates": self.check_updates,
                        "start_at_login": self.start_at_login}, indent=2),
            encoding="utf-8")


def _installer_check_updates(default: bool = True) -> bool:
    """The "check for updates" choice the installer wrote to HKCU\\Software\\MAD68."""
    try:
        import winreg
    except ImportError:
        return default
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\OpenMAD") as key:
            value, _ = winreg.QueryValueEx(key, "CheckUpdates")
            return bool(value)
    except OSError:
        return default


def _launch_command() -> str:
    """The command the Run key should execute.

    Frozen by PyInstaller this is just the executable. From a source checkout it
    has to be pythonw plus the tray script, or signing in would pop a console.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    tray = Path(__file__).resolve().parent.parent.parent / "tools" / "tray.py"
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    return f'"{runner}" "{tray}"'


def get_start_at_login() -> bool:
    """Whether the Run entry exists. The registry, not settings.json, decides."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, RUN_KEY_NAME)
            return True
    except OSError:
        return False


def set_start_at_login(enabled: bool) -> bool:
    """Add or remove the Run entry. Returns the state actually achieved."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_KEY_NAME, 0, winreg.REG_SZ,
                                  _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_KEY_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        return get_start_at_login()
    return get_start_at_login()
