"""Application-aware profile switching for the MAD68 HE.

Watches the Windows foreground window and applies the profile whose rule matches
the active process. This is the capability the stock configurator does not have.

Switching is host-driven rather than firmware-driven: the DuckBread controller
exposes no per-application command (the vendor's other Fire family does, via
GetAppDefine/SetAppDefine, but this keyboard has no equivalent).

Two design constraints shape this module:

* Flash endurance. Applying a profile writes to non-volatile memory with
  finite erase/write cycles. So: the active profile is cached by name and a
  re-apply is skipped entirely when nothing changed, and when a switch does
  happen only the differing fields are written.
* Alt-tab churn. Without debouncing, cycling through windows would fire a
  burst of writes. A candidate profile must stay stable for debounce_ms before
  it is applied, and min_dwell_ms rate-limits consecutive switches.
"""

from __future__ import annotations

import ctypes
import fnmatch
import json
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .device import Mad68
from .profile import Profile

CONFIG_VERSION = 1


# Foreground window inspection (Windows)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class ForegroundApp:
    """The process owning the foreground window."""

    exe: str  # basename, e.g. "cs2.exe"
    path: str  # full path, "" if unavailable
    title: str

    def __str__(self) -> str:
        return f"{self.exe} ({self.title[:40]})" if self.title else self.exe


def foreground_app() -> ForegroundApp | None:
    """Inspect the current foreground window. Windows only."""
    if sys.platform != "win32":
        raise RuntimeError("foreground_app() requires Windows")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ForegroundApp(exe="", path="", title=title)

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    path = ""
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            kernel32.CloseHandle(handle)

    exe = Path(path).name if path else ""
    return ForegroundApp(exe=exe, path=path, title=title)


def list_windowed_processes() -> list[dict]:
    """Every process that owns a visible top-level window, deduplicated by exe.

    This is what makes "bind a profile to an app" usable: pick the game from a
    list of what is actually running rather than typing an executable name and
    hoping it matches.
    """
    if sys.platform != "win32":
        raise RuntimeError("list_windowed_processes() requires Windows")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    found: dict[str, dict] = {}

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            return True
        try:
            pbuf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(pbuf))
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, pbuf, ctypes.byref(size)
            ):
                return True
            path = pbuf.value
        finally:
            kernel32.CloseHandle(handle)

        exe = Path(path).name
        if not exe:
            return True
        entry = found.setdefault(
            exe.lower(), {"exe": exe, "path": path, "titles": []}
        )
        if title not in entry["titles"]:
            entry["titles"].append(title)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), None)

    out = list(found.values())
    for e in out:
        e["title"] = e["titles"][0] if e["titles"] else ""
    out.sort(key=lambda e: e["exe"].lower())
    return out


# Rules


@dataclass
class Rule:
    """Maps a foreground app to a profile.

    exe is matched case-insensitively against the executable basename and
    supports glob patterns ("cs2.exe", "*.exe"). title_contains matches a
    case-insensitive substring of the window title. A rule with both requires
    both to match.
    """

    profile: str
    exe: str | None = None
    title_contains: str | None = None

    def matches(self, app: ForegroundApp) -> bool:
        if self.exe is not None:
            if not fnmatch.fnmatch(app.exe.lower(), self.exe.lower()):
                return False
        if self.title_contains is not None:
            if self.title_contains.lower() not in app.title.lower():
                return False
        return self.exe is not None or self.title_contains is not None

    def to_json(self) -> dict:
        d: dict = {"profile": self.profile}
        if self.exe is not None:
            d["exe"] = self.exe
        if self.title_contains is not None:
            d["title_contains"] = self.title_contains
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Rule":
        return cls(
            profile=d["profile"],
            exe=d.get("exe"),
            title_contains=d.get("title_contains"),
        )


@dataclass
class SwitcherConfig:
    """Rules plus timing knobs."""

    # Empty, not the name of a profile. A fresh install has no fallback until
    # the user picks one, and resolve() reads "" as "leave the keyboard alone".
    # This used to default to "stock", which no build has ever shipped, so a
    # first run failed to apply it, reported "profile 'stock' not found" and
    # left the tray icon showing the red error state.
    default_profile: str = ""
    rules: list[Rule] = field(default_factory=list)
    poll_interval_ms: int = 400
    debounce_ms: int = 800
    min_dwell_ms: int = 3000
    enabled: bool = True

    def resolve(self, app: ForegroundApp | None) -> str | None:
        """The profile that should be active for app. First match wins.

        Returns None when nothing matches and no default profile is set. That
        is a real state, not an error: the user is allowed to have no fallback,
        in which case an unrecognised app leaves the keyboard on whatever is
        already loaded rather than being switched to a profile named "".
        """
        if app is not None:
            for rule in self.rules:
                if rule.matches(app):
                    return rule.profile
        return self.default_profile or None

    def to_json(self) -> dict:
        return {
            "config_version": CONFIG_VERSION,
            "enabled": self.enabled,
            "default_profile": self.default_profile,
            "poll_interval_ms": self.poll_interval_ms,
            "debounce_ms": self.debounce_ms,
            "min_dwell_ms": self.min_dwell_ms,
            "rules": [r.to_json() for r in self.rules],
        }

    @classmethod
    def from_json(cls, d: dict) -> "SwitcherConfig":
        # Older files load; only a file from a *newer* app is refused.
        #
        # This used to demand an exact match, which made every future version
        # bump silently discard the settings of everyone upgrading: the file
        # became unreadable, the tray fell back to defaults, and the user's
        # rules were gone. Every field below already has a default, so a v1
        # file read by a v2 app simply leaves the new fields unset -- the same
        # forward compatibility Profile.from_json has always had.
        #
        # A config written by a newer version is the one case worth refusing,
        # since it may mean something by a field this code would misread. The
        # caller keeps the file rather than overwriting it.
        version = d.get("config_version")
        if not isinstance(version, int) or version > CONFIG_VERSION:
            raise ValueError(
                f"unsupported config_version {version!r}, expected {CONFIG_VERSION} "
                f"or older"
            )
        return cls(
            default_profile=d.get("default_profile", "") or "",
            rules=[Rule.from_json(r) for r in d.get("rules", [])],
            poll_interval_ms=int(d.get("poll_interval_ms", 400)),
            debounce_ms=int(d.get("debounce_ms", 800)),
            min_dwell_ms=int(d.get("min_dwell_ms", 3000)),
            enabled=bool(d.get("enabled", True)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SwitcherConfig":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))


# Switcher


@dataclass
class SwitchEvent:
    """Something the switcher did or decided, for logging and the tray UI."""

    when: float
    app: ForegroundApp | None
    profile: str
    writes: int
    note: str = ""


class Switcher:
    """Polls the foreground window and applies matching profiles.

    The device handle is opened per switch rather than held open, so the tray app
    and any other tool can coexist without fighting over the HID interface.
    """

    def __init__(self, config: SwitcherConfig, profile_dir: Path, *,
                 on_event: Callable[[SwitchEvent], None] | None = None,
                 dry_run: bool = False):
        self.config = config
        self.profile_dir = Path(profile_dir)
        self.on_event = on_event
        self.dry_run = dry_run

        self.active_profile: str | None = None
        self.current_app: ForegroundApp | None = None
        self.last_switch_at: float = 0.0
        self._candidate: str | None = None
        self._candidate_since: float = 0.0
        self._stop = False

        # Failure backoff. Without this, a persistent error (keyboard unplugged,
        # unreadable profile) makes every poll retry immediately and floods both
        # the log and the HID interface.
        self._retry_after: float = 0.0
        self._consecutive_failures: int = 0

    # profile loading

    def load_profile(self, name: str) -> Profile:
        return Profile.load(self.profile_dir / f"{name}.json")

    def available_profiles(self) -> list[str]:
        if not self.profile_dir.exists():
            return []
        return sorted(p.stem for p in self.profile_dir.glob("*.json"))

    # applying

    # Backoff after a failed apply, in seconds, then capped at the last value.
    BACKOFF_SECONDS = (2, 5, 15, 60)

    def _note_failure(self, now: float) -> None:
        idx = min(self._consecutive_failures, len(self.BACKOFF_SECONDS) - 1)
        self._retry_after = now + self.BACKOFF_SECONDS[idx]
        self._consecutive_failures += 1

    def _note_success(self, now: float, name: str) -> None:
        self.active_profile = name
        self.last_switch_at = now
        self._retry_after = 0.0
        self._consecutive_failures = 0

    def apply_profile(self, name: str, *, force: bool = False) -> SwitchEvent:
        """Apply name, skipping entirely if it is already active."""
        now = time.monotonic()
        if not force and self.active_profile == name:
            return self._emit(SwitchEvent(now, self.current_app, name, 0, "already active"))

        try:
            profile = self.load_profile(name)
        except FileNotFoundError:
            self._note_failure(now)
            return self._emit(
                SwitchEvent(now, self.current_app, name, 0, f"profile '{name}' not found")
            )
        except Exception as exc:
            self._note_failure(now)
            return self._emit(
                SwitchEvent(now, self.current_app, name, 0, f"profile '{name}' unreadable: {exc}")
            )

        try:
            with Mad68(writes=not self.dry_run) as kb:
                plan = profile.plan(kb)
                if plan.is_empty:
                    # Device already matches; record the name so we stop diffing.
                    self._note_success(now, name)
                    return self._emit(
                        SwitchEvent(now, self.current_app, name, 0, "device already matched")
                    )
                if self.dry_run:
                    # Treat a dry run as success so the loop settles instead of
                    # re-planning the same switch on every poll.
                    self._note_success(now, name)
                    return self._emit(
                        SwitchEvent(now, self.current_app, name, 0,
                                    f"dry run: would send {plan.packet_estimate} packet(s)")
                    )
                packets = plan.execute(kb)
                self._note_success(now, name)
                return self._emit(SwitchEvent(now, self.current_app, name, packets))
        except Exception as exc:
            self._note_failure(now)
            retry_in = max(0.0, self._retry_after - now)
            return self._emit(
                SwitchEvent(now, self.current_app, name, 0,
                            f"apply failed: {exc} (retry in {retry_in:.0f}s)")
            )

    def _emit(self, event: SwitchEvent) -> SwitchEvent:
        if self.on_event:
            self.on_event(event)
        return event

    # polling loop

    def poll_once(self) -> None:
        """One iteration: read foreground app, debounce, switch if warranted."""
        if not self.config.enabled:
            return

        try:
            app = foreground_app()
        except Exception:
            return
        self.current_app = app

        want = self.config.resolve(app)
        if want is None:
            # No rule matched and no fallback is configured, leave whatever is
            # loaded in place.
            self._candidate = None
            return
        now = time.monotonic()

        if want != self._candidate:
            # A new candidate resets the debounce window, and clears any backoff
            # so a different profile is not punished for the previous failure.
            self._candidate = want
            self._candidate_since = now
            self._retry_after = 0.0
            self._consecutive_failures = 0
            return

        if want == self.active_profile:
            return
        if now < self._retry_after:
            return
        if (now - self._candidate_since) * 1000 < self.config.debounce_ms:
            return
        if (now - self.last_switch_at) * 1000 < self.config.min_dwell_ms:
            return

        self.apply_profile(want)

    def run(self) -> None:
        """Blocking poll loop. Call stop() from another thread to end it."""
        self._stop = False
        while not self._stop:
            self.poll_once()
            time.sleep(self.config.poll_interval_ms / 1000)

    def stop(self) -> None:
        self._stop = True
