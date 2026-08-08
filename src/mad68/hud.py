"""Local web app: device home page + full-feature HUD.

Architecture: one Sampler thread owns the HID handle. It refreshes a telemetry
snapshot continuously and also executes queued work items, so HTTP handlers never
touch the device directly and concurrent requests cannot interleave HID traffic.

Served on 127.0.0.1 only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import protocol as proto
from .device import Mad68, RapidTrigger, find_config_interface, in_bootloader
from .features import AdvancedKey, BoxLight, DeadBand, Dks, DksAction
from .features import KeyboardFeature, LightInfo, MacroStep
from .keycodes import label
from .paths import assets_dir
from .profile import (
    PER_KEY_EFFECT,
    Profile,
    default_profile,
    ensure_default_profile,
)
from .settings import AppSettings, get_start_at_login, set_start_at_login
from .version import APP_NAME, APP_VERSION, GITHUB_REPO, PROJECT_URL
from .switcher import Rule, SwitcherConfig, list_windowed_processes
from .protocol import (
    MATRIX_COLS,
    MATRIX_ROWS,
    PRODUCT_ID,
    TOTAL_KEYS,
    VENDOR_ID,
    AdvancedKeyMode,
)

NOISE_FLOOR = 25
ASSUMED_RANGE = 500

# Full switch travel in 0.01 mm units, confirmed on hardware.
FULL_TRAVEL_RAW = 350

ADVANCED_KEY_SLOTS = 16
DKS_SLOTS = 16

# Focused-key sampling: single-key reads between full sweeps, and how many
# samples the gauge keeps for its trace.
# 
# There is deliberately no sleep between focus reads. Windows' timer
# granularity is ~15 ms, so even a 4 ms sleep costs 15 ms and starves the full
# sweep down to a few Hz. The USB round trip (~0.5 ms) paces the loop on its
# own, which keeps both the gauge and the heatmap responsive.
FOCUS_BURST = 12
FOCUS_TRACE_LEN = 160

# Records which profile was last committed to flash. The keyboard cannot be
# asked "which profile is in your flash?", flash just holds values, so the
# only way to name it is to remember what we wrote. Verified on load by diffing.
ONBOARD_STATE = "onboard.json"


def _onboard_path(profile_dir: Path) -> Path:
    return Path(profile_dir).parent / ONBOARD_STATE


def read_onboard(profile_dir: Path) -> dict:
    p = _onboard_path(profile_dir)
    if not p.exists():
        return {"profile": None, "committed_utc": None}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"profile": None, "committed_utc": None}


def write_onboard(profile_dir: Path, name: str) -> dict:
    import datetime
    state = {
        "profile": name,
        "committed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _onboard_path(profile_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


@dataclass
class Snapshot:
    keys: list[dict] = field(default_factory=list)
    calibrated: int = 0
    total: int = TOTAL_KEYS
    sample_hz: float = 0.0
    samples: int = 0
    active_profile: str | None = None
    profiles: list[str] = field(default_factory=list)
    error: str | None = None
    sweep_ms: float = 0.0
    connected: bool = False
    focus: dict | None = None

    def to_json(self) -> dict:
        return {
            "keys": self.keys,
            "calibrated": self.calibrated,
            "total": self.total,
            "sample_hz": round(self.sample_hz, 1),
            "samples": self.samples,
            "active_profile": self.active_profile,
            "profiles": self.profiles,
            "error": self.error,
            "sweep_ms": round(self.sweep_ms, 1),
            "connected": self.connected,
            "focus": self.focus,
        }


class _Job:
    """A unit of work for the device thread."""

    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error: str | None = None


class Sampler:
    def __init__(self, profile_dir: Path, interval: float = 0.03):
        self.profile_dir = Path(profile_dir)
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._stop = threading.Event()
        self._jobs: "queue.Queue[_Job]" = queue.Queue()

        self.names: list[str] = []
        self.calibration: list[int] = []
        self.baseline: list[int] = []
        self.observed_range: list[int] = []
        self.active_profile: str | None = None

        # When the UI is editing one key, that key is sampled on its own between
        # full sweeps. A full 75-key sweep is ~6 ms, which caps the refresh at
        # roughly 20 Hz, too coarse for a travel gauge you watch while pressing.
        # A single key is one packet, so the focused key updates far faster.
        self.focus: tuple[int, int] | None = None
        self.focus_trace: list[float] = []

    # lifecycle

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def reset_ranges(self) -> None:
        with self._lock:
            self.baseline = []
            self.observed_range = []

    def available_profiles(self) -> list[str]:
        if not self.profile_dir.exists():
            return []
        return sorted(p.stem for p in self.profile_dir.glob("*.json"))

    # work queue

    def submit(self, fn, timeout: float = 20.0):
        """Run fn(kb) on the device thread and return its result."""
        job = _Job(fn)
        self._jobs.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError("device thread did not respond")
        if job.error:
            raise RuntimeError(job.error)
        return job.result

    def _drain_jobs(self, kb: Mad68) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            try:
                job.result = job.fn(kb)
            except Exception as exc:
                job.error = str(exc)
            finally:
                job.done.set()

    # sampling

    def _init_static(self, kb: Mad68) -> None:
        keymap = kb.read_keymap()
        self.names = []
        for r in range(MATRIX_ROWS):
            for c in range(MATRIX_COLS):
                off = (r * MATRIX_COLS + c) * 2
                self.names.append(label(int.from_bytes(keymap[off:off + 2], "big")))
        self.calibration = kb.read_calibration_status()

    def _detect_active_profile(self, kb: Mad68) -> str | None:
        for name in self.available_profiles():
            try:
                if Profile.load(self.profile_dir / f"{name}.json").plan(kb).is_empty:
                    return name
            except Exception:
                continue
        return None

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with Mad68(writes=True) as kb:
                    self._init_static(kb)
                    self.active_profile = self._detect_active_profile(kb)
                    backoff = 1.0
                    self._sample_loop(kb)
            except Exception as exc:
                with self._lock:
                    self._snapshot = Snapshot(
                        error=str(exc), connected=False,
                        profiles=self.available_profiles(),
                        active_profile=self.active_profile,
                    )
                # Fail any queued jobs rather than letting callers hang.
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    job.error = f"device unavailable: {exc}"
                    job.done.set()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)

    def _sample_loop(self, kb: Mad68) -> None:
        count = 0
        window_start = time.monotonic()
        window_count = 0
        hz = 0.0

        while not self._stop.is_set():
            self._drain_jobs(kb)

            # While a key is focused, spend most iterations on it alone: one
            # packet instead of fourteen, so the travel gauge tracks the finger
            # instead of stepping. A full sweep still runs periodically to keep
            # the heatmap and the rest of the UI live.
            focus = self.focus
            if focus is not None:
                for _ in range(FOCUS_BURST):
                    if self._stop.is_set():
                        break
                    row, col = focus
                    try:
                        mm = kb.read_trip_mm(row, col)
                    except Exception:
                        break
                    self.focus_trace.append(mm)
                    del self.focus_trace[:-FOCUS_TRACE_LEN]
                    with self._lock:
                        snap = self._snapshot
                        snap.focus = {
                            "row": row, "col": col, "mm": mm,
                            "trace": list(self.focus_trace),
                        }
                    self._drain_jobs(kb)

            t0 = time.monotonic()
            adc = kb.read_adc_raw()
            trip = kb.read_trip_raw()
            sweep_ms = (time.monotonic() - t0) * 1000

            if not self.baseline:
                self.baseline = list(adc)
                self.observed_range = [ASSUMED_RANGE] * TOTAL_KEYS

            keys = []
            for i in range(TOTAL_KEYS):
                delta = adc[i] - self.baseline[i]
                mag = abs(delta)
                if mag > self.observed_range[i]:
                    self.observed_range[i] = mag
                span = max(self.observed_range[i], ASSUMED_RANGE)
                raw_trip = trip[i] if i < len(trip) else 0

                if raw_trip > 0:
                    travel_mm = raw_trip / 100.0
                    travel = min(1.0, raw_trip / FULL_TRAVEL_RAW)
                    source = "trip"
                elif mag > NOISE_FLOOR:
                    travel = min(1.0, mag / span)
                    travel_mm = round(travel * FULL_TRAVEL_RAW / 100.0, 2)
                    source = "sensor"
                else:
                    travel = 0.0
                    travel_mm = 0.0
                    source = "rest"

                keys.append({
                    "i": i,
                    "row": i // MATRIX_COLS,
                    "col": i % MATRIX_COLS,
                    "name": self.names[i] if i < len(self.names) else "?",
                    "adc": adc[i],
                    "rest": self.baseline[i],
                    "delta": delta,
                    "trip": raw_trip,
                    "travel": round(travel, 3),
                    "mm": round(travel_mm, 2),
                    "src": source,
                    "cal": bool(self.calibration[i]) if i < len(self.calibration) else False,
                })

            count += 1
            window_count += 1
            now = time.monotonic()
            if now - window_start >= 1.0:
                hz = window_count / (now - window_start)
                window_start = now
                window_count = 0

            with self._lock:
                self._snapshot = Snapshot(
                    keys=keys,
                    calibrated=sum(1 for v in self.calibration if v),
                    total=TOTAL_KEYS,
                    sample_hz=hz,
                    samples=count,
                    active_profile=self.active_profile,
                    profiles=self.available_profiles(),
                    sweep_ms=sweep_ms,
                    connected=True,
                    focus=self._snapshot.focus,
                )

            # Focused mode runs hot on purpose, but not flat out, a small
            # yield keeps USB and CPU sane while still leaving the gauge far
            # smoother than the eye needs.
            self._stop.wait(0.005 if self.focus is not None else self.interval)


# Device-thread operations


def read_full_config(kb: Mad68) -> dict:
    """Everything the settings tabs need, in one device-thread pass."""
    keymap = kb.read_keymap()
    layers = []
    for layer in range(4):
        row_out = []
        for r in range(MATRIX_ROWS):
            cells = []
            for c in range(MATRIX_COLS):
                off = ((layer * MATRIX_ROWS + r) * MATRIX_COLS + c) * 2
                kc = int.from_bytes(keymap[off:off + 2], "big")
                cells.append({"kc": kc, "name": label(kc)})
            row_out.append(cells)
        layers.append(row_out)

    def safe(fn, default=None):
        try:
            return fn()
        except Exception as exc:
            return {"error": str(exc)} if default is None else default

    advanced = []
    for i in range(ADVANCED_KEY_SLOTS):
        try:
            ak = kb.read_advanced_key(i)
            if ak.is_active:
                advanced.append(ak.to_json())
        except Exception:
            break

    dks = []
    for i in range(DKS_SLOTS):
        try:
            d = kb.read_dks(i)
            if not d.is_empty:
                dks.append(d.to_json())
        except Exception:
            break

    try:
        macros = [[s.to_json() for s in steps] for steps in kb.read_macro_steps()]
    except Exception as exc:
        macros = [{"error": str(exc)}]

    proto_version = safe(kb.protocol_version, 0)

    return {
        "actuation": kb.read_actuation_bulk(),
        "rapid_trigger": [rt.to_json() for rt in kb.read_rapid_trigger_bulk()],
        "key_colors": [list(c) for c in safe(kb.read_all_key_colors, [])],
        "light": safe(lambda: kb.read_light_info().to_json()),
        "box_light": safe(lambda: kb.read_box_light().to_json()),
        "dead_band": safe(lambda: kb.read_dead_band().to_json()),
        "feature": safe(lambda: kb.read_feature().to_json()),
        "game_mode": safe(kb.read_game_mode, 0),
        "bottom_optimize": safe(kb.read_bottom_optimize, 0),
        "layers": layers,
        "advanced_keys": advanced,
        "dks": dks,
        "macros": macros,
        "protocol_version": proto_version,
        "protocol_version_known": proto_version == proto.KNOWN_PROTOCOL_VERSION,
        "macro_count": safe(kb.macro_count, 0),
        "macro_buffer_size": safe(kb.macro_buffer_size, 0),
    }


# Native Win32 file dialogs, straight from comdlg32.
#
# These used to run Tk inside a subprocess spawned from sys.executable. That
# works from a source checkout and cannot work from a build: PyInstaller sets
# sys.executable to OpenMAD.exe, not to a Python interpreter, so "Browse for
# .exe" launched a second copy of the app, got no path back on stdout and
# returned None -- the picker never appeared at all. ctypes calls the same
# dialog Explorer uses, needs neither an interpreter nor Tk, and behaves
# identically frozen and unfrozen.

_OFN_OVERWRITEPROMPT = 0x00000002
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_EXPLORER = 0x00080000


def _ofn_filter(pairs: list[tuple[str, str]]) -> str:
    """comdlg32 wants label/pattern pairs NUL-separated and NUL-NUL-terminated."""
    return "".join(f"{label}\0{pattern}\0" for label, pattern in pairs) + "\0"


def _file_dialog(title: str, filters: list[tuple[str, str]], *, save: bool = False,
                 default_name: str = "", default_ext: str = "") -> str | None:
    """Show an open or save dialog and return the chosen path, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        # restype matters on 64-bit: the default c_int truncates the HWND.
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        fn = comdlg32.GetSaveFileNameW if save else comdlg32.GetOpenFileNameW
        fn.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        fn.restype = wintypes.BOOL

        buf = ctypes.create_unicode_buffer(default_name, 32768)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        # Owned by whatever is in front -- normally the browser the HUD is open
        # in -- so the dialog comes up over it instead of only blinking in the
        # taskbar. This process is not the foreground one when the click
        # arrives, so an unowned dialog would not reliably raise itself.
        ofn.hwndOwner = user32.GetForegroundWindow()
        ofn.lpstrFilter = _ofn_filter(filters)
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = 32768
        ofn.lpstrTitle = title
        ofn.lpstrDefExt = default_ext or None
        # NOCHANGEDIR because the dialog otherwise moves the whole process's
        # working directory to wherever the user browsed.
        ofn.Flags = (_OFN_EXPLORER | _OFN_HIDEREADONLY | _OFN_NOCHANGEDIR
                     | _OFN_PATHMUSTEXIST
                     | (_OFN_OVERWRITEPROMPT if save else _OFN_FILEMUSTEXIST))
        # Cancelling returns 0 and is not an error, so the result is simply the
        # absence of a path.
        if not fn(ctypes.byref(ofn)):
            return None
        return buf.value or None
    except Exception:
        return None


def browse_for_exe() -> str | None:
    """Open a native file picker for an executable."""
    return _file_dialog(
        "Pick the game or app executable",
        [("Executables", "*.exe"), ("All files", "*.*")],
    )


def browse_open_json(title: str) -> str | None:
    return _file_dialog(
        title, [("Profile files", "*.json"), ("All files", "*.*")])


def browse_save_json(title: str, default_name: str) -> str | None:
    return _file_dialog(
        title, [("Profile files", "*.json")],
        save=True, default_name=default_name, default_ext="json")


# GITHUB_REPO and APP_VERSION come from version.ini via .version, which the
# installer script also reads. Do not hardcode either here.


def _version_tuple(tag: str) -> tuple:
    """Parse 'v1.2.3' into (1, 2, 3) for comparison. Unparsable parts sort low."""
    parts = re.findall(r"\d+", tag or "")
    return tuple(int(p) for p in parts) or (0,)


def check_for_update(timeout: float = 6.0) -> dict:
    """Ask GitHub whether a newer tagged release exists.

    Read-only and best-effort: no token, no telemetry, and any failure (offline,
    rate-limited, repo missing) is reported as "no update" rather than raised,
    because a background check must never interrupt the app.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "mad68-driver"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"checked": False, "update": False, "error": str(exc),
                "current": APP_VERSION}
    tag = data.get("tag_name") or ""
    newer = _version_tuple(tag) > _version_tuple(APP_VERSION)
    return {
        "checked": True,
        "update": newer,
        "current": APP_VERSION,
        "latest": tag.lstrip("v"),
        "url": data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases",
        "notes": (data.get("body") or "")[:600],
    }


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


def _safe_profile_name(name) -> str:
    """Profile names become filenames, so keep them boring and path-free."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name.strip()):
        raise ValueError(
            "profile name must be 1-40 chars of letters, digits, space, - or _"
        )
    return name.strip()


def _keymap_overview(profile_dir: Path, kb: Mad68, name: str) -> dict:
    """How every profile's stored keymap compares to the one on the keyboard.

    One board read, compared against all profiles, so the UI can say which
    other profiles are safe to switch to without the key bindings changing
    under the user.
    """
    live = kb.read_keymap()
    live_hex = live.hex()

    same, differ, none = [], [], []
    for path in sorted(Path(profile_dir).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        stored = doc.get("keymap_hex")
        if not stored:
            none.append(path.stem)
        elif stored == live_hex:
            same.append(path.stem)
        else:
            differ.append(path.stem)

    out = {"same": same, "differs": differ, "no_keymap": none}
    if name:
        dest = Path(profile_dir) / f"{name}.json"
        if dest.exists():
            try:
                prof = Profile.load(dest)
                out["profile"] = prof.keymap_status(kb)
            except Exception as exc:
                out["profile"] = {"error": str(exc)}
    return out


def _switcher_references(profile_dir: Path, name: str) -> list[str]:
    """Which switcher rules mention this profile, so deleting it can warn."""
    cfg = Path(profile_dir).parent / "switcher.json"
    if not cfg.exists():
        return []
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    hits = []
    if data.get("default_profile") == name:
        hits.append("default_profile")
    for rule in data.get("rules", []):
        if rule.get("profile") == name:
            hits.append(rule.get("exe") or rule.get("title_contains") or "rule")
    return hits


def _doc_triggers(doc: dict) -> list[dict]:
    """A profile document's triggers, in either schema.

    triggers is the list; trigger_exe/trigger_title are the single-binding
    fields it replaced and are still honoured so older profiles keep working.
    """
    raw = doc.get("triggers")
    if raw is None:
        exe = (doc.get("trigger_exe") or "").strip()
        title = (doc.get("trigger_title") or "").strip()
        raw = [{"exe": exe, "title": title}] if (exe or title) else []
    out, seen = [], set()
    for t in raw:
        exe = (t.get("exe") or "").strip()
        title = (t.get("title") or "").strip()
        if not (exe or title):
            continue
        key = (exe.lower(), title.lower())
        if key in seen:  # a duplicate rule can never fire; first match wins
            continue
        seen.add(key)
        out.append({"exe": exe, "title": title})
    return out


def _sync_rule_for(profile_dir: Path, name: str, doc: dict) -> None:
    """Mirror a profile's trigger apps into switcher.json.

    The profile owns "which apps launch me", but the tray reads switcher.json,
    so the two are kept in step here rather than making the user edit both. A
    profile can name several apps and gets one rule each.
    """
    cfg_path = Path(profile_dir).parent / "switcher.json"
    try:
        cfg = SwitcherConfig.load(cfg_path) if cfg_path.exists() else SwitcherConfig()
    except Exception:
        cfg = SwitcherConfig()
    triggers = _doc_triggers(doc)
    exes = {t["exe"].lower() for t in triggers if t["exe"] and not t["title"]}

    # Drop this profile's old rules, and any other rule claiming an executable
    # we are about to take, first match wins, so a duplicate would silently be
    # dead and the user would see their new binding do nothing.
    def clashes(r) -> bool:
        if r.profile == name:
            return True
        return bool(r.exe) and r.exe.lower() in exes and not r.title_contains

    cfg.rules = [r for r in cfg.rules if not clashes(r)]
    cfg.rules += [Rule(profile=name, exe=t["exe"] or None,
                       title_contains=t["title"] or None) for t in triggers]
    cfg.save(cfg_path)


def _emergency_backup(kb, profile_dir: Path) -> str:
    """Minimal snapshot written straight before a destructive operation."""
    import datetime
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(profile_dir).parent / "backups" / f"pre-factory-reset-{stamp}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": "taken automatically immediately before a factory reset",
        "keymap_hex": kb.read_keymap().hex(),
        "macros_hex": kb.read_macros().hex(),
        "actuation": kb.read_actuation_bulk(),
        "rapid_trigger": [r.to_json() for r in kb.read_rapid_trigger_bulk()],
        "advanced_keys": [kb.read_advanced_key(i).to_json()
                          for i in range(ADVANCED_KEY_SLOTS)],
        "key_colors": [list(c) for c in kb.read_all_key_colors()],
        "light": kb.read_light_info().to_json(),
    }
    dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(dest)


def _macro_step(d: dict) -> MacroStep:
    kind = d.get("kind")
    if kind == "text":
        return MacroStep("text", text=str(d.get("text", "")))
    if kind == "delay":
        return MacroStep("delay", delay_ms=int(d.get("delay_ms", 0)))
    if kind in ("tap", "down", "up"):
        return MacroStep(kind, keycode=int(d.get("keycode", 0)))
    raise ValueError(f"unknown macro step kind {kind!r}")


def _persist_gain(gain: tuple[float, float, float]) -> bool:
    """Write the tuned gain into protocol.py so it survives a restart."""
    path = Path(__file__).resolve().parent / "protocol.py"
    try:
        text = path.read_text(encoding="utf-8")
        new = (f"LED_CHANNEL_GAIN = ({round(gain[0], 3)}, {round(gain[1], 3)}, "
               f"{round(gain[2], 3)})")
        updated, n = re.subn(r"^LED_CHANNEL_GAIN = \([^)]*\)$", new, text,
                             count=1, flags=re.M)
        if n != 1:
            return False
        path.write_text(updated, encoding="utf-8")
        return True
    except Exception:
        return False


def build_stamp() -> dict:
    """Identifies this server: is it ours, and is it running current code?

    A long-lived HTTP server keeps the page and every module constant in memory
    from the moment it started. Reusing whatever happens to be listening on the
    port therefore serves stale HTML and stale settings indefinitely, which is
    exactly what an hour-old server did, showing old styling and applying an LED
    gain that had since been reverted. The launcher compares this stamp and
    restarts the server when it does not match.
    """
    import hashlib
    import os
    src = (PAGE_HUD + PAGE_HOME + repr(proto.LED_CHANNEL_GAIN)
           + repr(proto.LED_CHANNEL_ORDER))
    return {
        "app": "openmad-hud",
        "pid": os.getpid(),
        "build": hashlib.sha256(src.encode()).hexdigest()[:12],
    }


def device_identity() -> dict:
    """Detect the keyboard without opening it for I/O."""
    try:
        iface = find_config_interface()
        return {
            "present": True,
            "vendor_id": f"{VENDOR_ID:#06x}",
            "product_id": f"{PRODUCT_ID:#06x}",
            "product": iface.get("product_string"),
            "manufacturer": iface.get("manufacturer_string"),
            "serial": iface.get("serial_number"),
            "bootloader": False,
        }
    except Exception as exc:
        return {
            "present": False,
            "bootloader": in_bootloader(),
            "reason": str(exc),
            "vendor_id": f"{VENDOR_ID:#06x}",
            "product_id": f"{PRODUCT_ID:#06x}",
        }


# HTTP


def make_handler(sampler: Sampler):
    class Handler(BaseHTTPRequestHandler):
        server_version = "openmad-hud"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE_HOME.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/hud":
                self._send(200, PAGE_HUD.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/led-gain":
                g = proto.LED_CHANNEL_GAIN
                self._json({"r": g[0], "g": g[1], "b": g[2]})
            elif path == "/api/version":
                # Lets a launcher tell "our server, current code" from "something
                # else on this port" or "a server left over from an older run".
                self._json(build_stamp())
            elif path == "/api/device":
                self._json(device_identity())
            elif path == "/api/state":
                body = sampler.snapshot().to_json()
                body["onboard"] = read_onboard(sampler.profile_dir)
                self._json(body)
            elif path in ("/logo.png", "/icon.png", "/icon.ico"):
                # Branding is dropped into assets/ rather than embedded. A
                # missing file is a 404 the page handles, not an error.
                # Read from the bundle, not from the data directory: this ships
                # with the app, so it does not sit beside the user's profiles.
                asset = assets_dir() / path.lstrip("/")
                if asset.exists():
                    kind = ("image/png" if path.endswith(".png")
                            else "image/x-icon")
                    self._send(200, asset.read_bytes(), kind)
                else:
                    self._send(404, b"", "image/png")
            elif path == "/api/settings":
                s_path = Path(sampler.profile_dir).parent / "settings.json"
                st = AppSettings.load(s_path)
                # The registry is authoritative for start-at-login: the user can
                # remove the entry from Task Manager behind our back.
                st.start_at_login = get_start_at_login()
                self._json({"check_updates": st.check_updates,
                            "start_at_login": st.start_at_login,
                            "version": APP_VERSION})
            elif path == "/api/keymap-status":
                # Which profiles share the keymap currently on the keyboard.
                # Read-only, and the answer drives the banner in Change Key
                # Setting rather than any automatic behaviour.
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                want = (q.get("name") or [""])[0]
                try:
                    self._json(sampler.submit(
                        lambda kb: _keymap_overview(sampler.profile_dir, kb, want),
                        timeout=40))
                except Exception as exc:
                    self._json({"error": str(exc)}, 503)
            elif path == "/api/update-check":
                self._json(check_for_update())
            elif path == "/api/profile":
                # path already has the query stripped, so match on the bare
                # path and pull the name out of self.path.
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                f = sampler.profile_dir / f"{name}.json"
                if not f.exists():
                    self._json({"error": "no such profile"}, 404)
                else:
                    self._json(json.loads(f.read_text(encoding="utf-8-sig")))
            elif path == "/api/profiles":
                out = []
                for name in sampler.available_profiles():
                    try:
                        p = Profile.load(sampler.profile_dir / f"{name}.json")
                        out.append({
                            "name": name, "description": p.description,
                            "actuation_default": p.actuation_default,
                            "actuation_overrides": len(p.actuation_overrides),
                            "rt_overrides": len(p.rapid_trigger_overrides),
                            "rt_default": p.rapid_trigger_default.to_json(),
                            "has_keymap": p.keymap_hex is not None,
                        })
                    except Exception as exc:
                        out.append({"name": name, "error": str(exc)})
                self._json({"profiles": out,
                            "onboard": read_onboard(sampler.profile_dir)})
            elif path == "/api/processes":
                try:
                    self._json({"processes": list_windowed_processes()})
                except Exception as exc:
                    self._json({"processes": [], "error": str(exc)})
            elif path == "/api/rules":
                cfg_path = Path(sampler.profile_dir).parent / "switcher.json"
                try:
                    cfg = SwitcherConfig.load(cfg_path) if cfg_path.exists() \
                        else SwitcherConfig()
                    self._json(cfg.to_json())
                except Exception as exc:
                    self._json({"error": str(exc)}, 500)
            elif path == "/api/browse-exe":
                self._json({"path": browse_for_exe()})
            elif path == "/api/config":
                try:
                    self._json(sampler.submit(read_full_config, timeout=30))
                except Exception as exc:
                    self._json({"error": str(exc)}, 503)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                d = json.loads(raw or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            path = self.path.split("?")[0]

            try:
                self._json({"ok": True, "result": self._dispatch(path, d)})
            except KeyError:
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def _dispatch(self, path: str, d: dict):
            s = sampler

            if path == "/api/shutdown":
                # Only used so a newer launcher can replace an out-of-date
                # server holding the port. Localhost-only, like everything here.
                threading.Timer(0.2, lambda: os._exit(0)).start()
                return {"stopping": True}

            if path == "/api/led-gain":
                # Live colour calibration. Changing the module global takes
                # effect on the very next write, so the board responds while you
                # drag a slider, which is the only practical way to tune this
                # given the person doing the tuning is the only one who can see
                # the result.
                gain = (float(d.get("r", 1.0)), float(d.get("g", 1.0)),
                        float(d.get("b", 1.0)))
                proto.LED_CHANNEL_GAIN = gain
                preview = d.get("preview")
                if preview:
                    rgb = (int(preview["r"]), int(preview["g"]), int(preview["b"]))

                    bright = min(int(d.get("brightness", proto.BRIGHTNESS_MAX)),
                                 proto.BRIGHTNESS_MAX)

                    def paint(kb):
                        kb.write_light_info(LightInfo(
                            effect=PER_KEY_EFFECT, speed=0, r=rgb[0], g=rgb[1],
                            b=rgb[2], brightness=bright))
                        kb.write_all_key_colors([rgb] * TOTAL_KEYS)
                        return True

                    s.submit(paint, timeout=30)
                if d.get("persist"):
                    _persist_gain(gain)
                return {"gain": gain, "persisted": bool(d.get("persist"))}

            if path == "/api/reset-ranges":
                s.reset_ranges()
                return None

            if path == "/api/apply-profile":
                name = d.get("profile")
                if name not in s.available_profiles():
                    raise ValueError("unknown profile")

                def apply(kb):
                    prof = Profile.load(s.profile_dir / f"{name}.json")
                    n = prof.plan(kb).execute(kb, persist=bool(d.get("persist")))
                    s.active_profile = name
                    return {"packets": n}

                return s.submit(apply, timeout=30)

            if path == "/api/actuation":
                mm = float(d["mm"])
                if d.get("all"):
                    return s.submit(
                        lambda kb: {"packets": kb.write_actuation_bulk([mm] * TOTAL_KEYS)}
                    )
                row, col = int(d["row"]), int(d["col"])
                return s.submit(lambda kb: kb.write_actuation_mm(row, col, mm))

            if path == "/api/rapid-trigger":
                enabled = bool(d["enabled"])
                press = float(d["press_mm"])
                release = float(d["release_mm"])
                if d.get("all"):
                    def w(kb):
                        cur = kb.read_rapid_trigger_bulk()
                        new = [RapidTrigger(row=r.row, col=r.col, enabled=enabled,
                                            press_mm=press, release_mm=release)
                               for r in cur]
                        return {"packets": kb.write_rapid_trigger_bulk(new)}
                    return s.submit(w)
                row, col = int(d["row"]), int(d["col"])
                return s.submit(lambda kb: kb.write_rapid_trigger(
                    RapidTrigger(row=row, col=col, enabled=enabled,
                                 press_mm=press, release_mm=release)))

            if path == "/api/light":
                info = LightInfo(effect=int(d["effect"]), speed=int(d["speed"]),
                                 r=int(d["r"]), g=int(d["g"]), b=int(d["b"]),
                                 brightness=int(d["brightness"]))
                return s.submit(lambda kb: kb.write_light_info(info))

            if path == "/api/box-light":
                box = BoxLight(mode=int(d["mode"]), colorful=int(d["colorful"]),
                               brightness=int(d["brightness"]), speed=int(d["speed"]))
                return s.submit(lambda kb: kb.write_box_light(box))

            if path == "/api/key-colors":
                if d.get("all"):
                    rgb = (int(d["r"]), int(d["g"]), int(d["b"]))
                    return s.submit(
                        lambda kb: {"packets": kb.write_all_key_colors([rgb] * TOTAL_KEYS)}
                    )
                row, col = int(d["row"]), int(d["col"])
                rgb = (int(d["r"]), int(d["g"]), int(d["b"]))
                return s.submit(lambda kb: kb.write_key_colors(row, col, [rgb]))

            if path == "/api/dead-band":
                band = DeadBand(top_mm=float(d["top_mm"]), bottom_mm=float(d["bottom_mm"]))
                return s.submit(lambda kb: kb.write_dead_band(band))

            if path == "/api/feature":
                f = KeyboardFeature(
                    rgb_area=int(d["rgb_area"]), wasd_switch=int(d["wasd_switch"]),
                    mac_switch=int(d["mac_switch"]), win_lock=int(d["win_lock"]),
                    nkro_switch=int(d["nkro_switch"]))
                return s.submit(lambda kb: kb.write_feature(f))

            if path == "/api/game-mode":
                v = int(d["value"])
                return s.submit(lambda kb: kb.write_game_mode(v))

            if path == "/api/bottom-optimize":
                v = int(d["value"])
                return s.submit(lambda kb: kb.write_bottom_optimize(v))

            if path == "/api/advanced-key":
                ak = AdvancedKey(
                    index=int(d["index"]), mode=AdvancedKeyMode(int(d["mode"])),
                    id=int(d.get("id", 0)), rs_apc_lv=int(d.get("rs_apc_lv", 0)),
                    gapc_sw=int(d.get("gapc_sw", 0)), rt_sw=int(d.get("rt_sw", 0)),
                    key1_row=int(d.get("key1_row", 0)), key1_col=int(d.get("key1_col", 0)),
                    key2_row=int(d.get("key2_row", 0)), key2_col=int(d.get("key2_col", 0)),
                    layer=int(d.get("layer", 0)))
                return s.submit(lambda kb: kb.write_advanced_key(ak))

            if path == "/api/keycode":
                layer, row, col = int(d["layer"]), int(d["row"]), int(d["col"])
                kc = int(d["keycode"])
                prof_name = d.get("profile")

                def remap(kb):
                    kb.write_keycode(layer, row, col, kc)
                    # Keep the editing profile's stored keymap in step with the
                    # board. Without this the edit would be reverted the next
                    # time that profile's keymap was applied.
                    if prof_name:
                        try:
                            path_ = s.profile_dir / f"{_safe_profile_name(prof_name)}.json"
                            if path_.exists():
                                doc = json.loads(path_.read_text(encoding="utf-8-sig"))
                                doc["keymap_hex"] = kb.read_keymap().hex()
                                path_.write_text(json.dumps(doc, indent=2),
                                                 encoding="utf-8")
                        except Exception:
                            # The board write already succeeded; failing to
                            # mirror it into the file must not report an error
                            # for something that did happen.
                            pass
                    return True

                return s.submit(remap)

            if path == "/api/profile/keymap/apply":
                # The one place a keymap reaches the keyboard. Deliberate, and
                # an EEPROM write, which is why nothing calls it automatically.
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if not dest.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                prof = Profile.load(dest)

                def push(kb):
                    return {"packets": prof.apply_keymap(kb)}

                return s.submit(push, timeout=60)

            if path == "/api/profile/keymap/capture":
                # Store whatever is currently on the board as this profile's
                # keymap. Touches no hardware.
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if not dest.exists():
                    raise ValueError(f"profile '{name}' does not exist")

                def grab(kb):
                    doc = json.loads(dest.read_text(encoding="utf-8-sig"))
                    doc["keymap_hex"] = kb.read_keymap().hex()
                    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                    return {"captured": True}

                return s.submit(grab, timeout=40)

            if path == "/api/save-flash":
                what = d.get("what", "he")
                if what == "lighting":
                    return s.submit(lambda kb: kb.save_lighting(), timeout=30)
                return s.submit(lambda kb: kb.commit_to_flash(), timeout=60)

            # profile management

            if path == "/api/profile/create":
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if dest.exists() and not d.get("overwrite"):
                    raise ValueError(f"profile '{name}' already exists")

                def cap(kb):
                    prof = Profile.from_device(
                        kb, name,
                        # Capture the keymap by default now that profiles own
                        # one. Applying it is still a deliberate action; storing
                        # it costs nothing.
                        include_keymap=bool(d.get("include_keymap", True)),
                        description=str(d.get("description") or ""))
                    prof.save(dest)
                    return {"name": name}

                return s.submit(cap, timeout=40)

            if path == "/api/profile/create-blank":
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if dest.exists():
                    raise ValueError(f"profile '{name}' already exists")
                prof = default_profile(name)
                prof.description = str(d.get("description") or "")
                prof.save(dest)
                return {"name": name}

            if path == "/api/profile/edit":
                # Profiles are the source of truth: patch the document, then push
                # it to the keyboard so what you feel is what the profile says.
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if not dest.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                doc = json.loads(dest.read_text(encoding="utf-8-sig"))
                patch = d.get("patch") or {}

                for key, value in patch.items():
                    if key == "actuation_keys":
                        # Bulk edit: the UI selects many keys and applies at once.
                        doc.setdefault("actuation", {}).setdefault("overrides_mm", {})
                        for item in value:
                            doc["actuation"]["overrides_mm"][item["key"]] = float(item["mm"])
                    elif key == "rt_keys":
                        doc.setdefault("rapid_trigger", {}).setdefault("overrides", {})
                        for item in value:
                            doc["rapid_trigger"]["overrides"][item["key"]] = {
                                "enabled": bool(item["enabled"]),
                                "press_mm": float(item["press_mm"]),
                                "release_mm": float(item["release_mm"])}
                    elif key == "key_colors_keys":
                        colors = doc.get("key_colors") or [[0, 0, 0]] * TOTAL_KEYS
                        for item in value:
                            colors[int(item["index"])] = [int(item["r"]), int(item["g"]),
                                                          int(item["b"])]
                        doc["key_colors"] = colors
                    elif key == "actuation_override":
                        doc.setdefault("actuation", {}).setdefault("overrides_mm", {})
                        kk, vv = value["key"], value["mm"]
                        if vv is None:
                            doc["actuation"]["overrides_mm"].pop(kk, None)
                        else:
                            doc["actuation"]["overrides_mm"][kk] = float(vv)
                    elif key == "actuation_default":
                        doc.setdefault("actuation", {})["default_mm"] = float(value)
                        doc["actuation"]["overrides_mm"] = {}
                    elif key == "rt_override":
                        doc.setdefault("rapid_trigger", {}).setdefault("overrides", {})
                        kk = value["key"]
                        if value.get("clear"):
                            doc["rapid_trigger"]["overrides"].pop(kk, None)
                        else:
                            doc["rapid_trigger"]["overrides"][kk] = {
                                "enabled": bool(value["enabled"]),
                                "press_mm": float(value["press_mm"]),
                                "release_mm": float(value["release_mm"])}
                    elif key == "rt_default":
                        doc.setdefault("rapid_trigger", {})["default"] = {
                            "enabled": bool(value["enabled"]),
                            "press_mm": float(value["press_mm"]),
                            "release_mm": float(value["release_mm"])}
                        doc["rapid_trigger"]["overrides"] = {}
                    elif key == "key_color":
                        colors = doc.get("key_colors") or [[0, 0, 0]] * TOTAL_KEYS
                        colors[int(value["index"])] = [int(value["r"]), int(value["g"]),
                                                       int(value["b"])]
                        doc["key_colors"] = colors
                    elif key == "key_colors_all":
                        doc["key_colors"] = [[int(value["r"]), int(value["g"]),
                                              int(value["b"])] for _ in range(TOTAL_KEYS)]
                    elif key in ("light", "performance"):
                        doc.setdefault(key, {})
                        doc[key].update({k2: v2 for k2, v2 in value.items()})
                    elif key == "tap_dance":
                        tds = doc.get("tap_dance") or []
                        by_i = {t["index"]: t for t in tds}
                        by_i[int(value["index"])] = value
                        doc["tap_dance"] = [by_i[i] for i in sorted(by_i)]
                    elif key == "dks":
                        entries = doc.get("dks") or []
                        by_i = {d2["index"]: d2 for d2 in entries}
                        by_i[int(value["index"])] = value
                        doc["dks"] = [by_i[i] for i in sorted(by_i)]
                    elif key == "advanced_key":
                        aks = doc.get("advanced_keys") or []
                        by_index = {a["index"]: a for a in aks}
                        by_index[int(value["index"])] = value
                        doc["advanced_keys"] = [by_index[i] for i in sorted(by_index)]
                    elif key in ("description", "triggers",
                                 "trigger_exe", "trigger_title"):
                        doc[key] = value
                        if key == "triggers":
                            # The legacy singles would otherwise shadow the
                            # list for anything reading the old schema.
                            doc.pop("trigger_exe", None)
                            doc.pop("trigger_title", None)
                    elif key == "keymap_hex":
                        doc["keymap_hex"] = value
                    else:
                        raise ValueError(f"unknown patch key {key!r}")

                dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                _sync_rule_for(s.profile_dir, name, doc)

                applied = 0
                if d.get("apply", True):
                    prof = Profile.load(dest)

                    def push(kb):
                        n = prof.plan(kb).execute(kb)
                        s.active_profile = name
                        return n

                    applied = s.submit(push, timeout=60)
                return {"applied_packets": applied}

            if path == "/api/profile/duplicate":
                name = _safe_profile_name(d.get("name"))
                new = _safe_profile_name(d.get("new_name"))
                src = s.profile_dir / f"{name}.json"
                dst = s.profile_dir / f"{new}.json"
                if not src.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                if dst.exists():
                    raise ValueError(f"profile '{new}' already exists")
                doc = json.loads(src.read_text(encoding="utf-8-sig"))
                doc["name"] = new
                doc.pop("triggers", None)
                doc.pop("trigger_exe", None)
                doc.pop("trigger_title", None)
                dst.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                return {"name": new}

            if path == "/api/calibrate":
                # Calibration is the one operation that can genuinely degrade a
                # Hall-effect board, so it needs the dangerous gate and a typed
                # confirmation, exactly like the factory reset.
                action = d.get("action")
                if action not in ("start", "finish"):
                    raise ValueError("action must be 'start' or 'finish'")
                if d.get("confirm") != "CALIBRATE":
                    raise ValueError("calibration needs confirm == 'CALIBRATE'")

                def run(_kb):
                    from .device import Mad68 as _M
                    from .protocol import Vendor as _V
                    sub = _V.CALIBRATION_START if action == "start" else _V.CALIBRATION_FINISH
                    with _M(writes=True, dangerous=True) as danger:
                        danger.vendor_set(sub)
                    return action

                return s.submit(run, timeout=60)

            if path == "/api/factory-reset":
                # Deliberately awkward: a typed confirmation, a backup first, and
                # the transport's dangerous gate. This wipes onboard settings.
                if d.get("confirm") != "ERASE":
                    raise ValueError("factory reset needs confirm == 'ERASE'")

                def wipe(_kb):
                    from .device import Mad68 as _M
                    with _M(writes=True, dangerous=True) as danger:
                        danger.cmd(Cmd.EEPROM_RESET)
                    return True

                # Snapshot first so there is a way back.
                backup = s.submit(lambda kb: _emergency_backup(kb, s.profile_dir),
                                  timeout=120)
                s.submit(wipe, timeout=60)
                return {"backup": backup}

            if path == "/api/profile/delete":
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if not dest.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                referenced = _switcher_references(s.profile_dir, name)
                dest.unlink()
                if s.active_profile == name:
                    s.active_profile = None
                return {"deleted": name, "referenced_by_rules": referenced}

            if path == "/api/profile/rename":
                name = _safe_profile_name(d.get("name"))
                new = _safe_profile_name(d.get("new_name"))
                src = s.profile_dir / f"{name}.json"
                dst = s.profile_dir / f"{new}.json"
                if not src.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                if dst.exists():
                    raise ValueError(f"profile '{new}' already exists")
                prof = Profile.load(src)
                prof.name = new
                prof.save(dst)
                src.unlink()
                if s.active_profile == name:
                    s.active_profile = new
                return {"renamed": [name, new],
                        "referenced_by_rules": _switcher_references(s.profile_dir, name)}

            if path == "/api/profile/set-onboard":
                name = _safe_profile_name(d.get("name"))
                if name not in s.available_profiles():
                    raise ValueError("unknown profile")

                def commit(kb):
                    prof = Profile.load(s.profile_dir / f"{name}.json")
                    # The only plan that carries the keymap. Writing a profile
                    # into onboard memory is meant to make the keyboard behave
                    # like that profile on its own, which includes its key
                    # bindings, and this is already an explicit flash write
                    # behind a confirmation.
                    p = prof.plan(kb, include_keymap=True)
                    if p.is_empty:
                        # The device already matches, so there is nothing to
                        # write, but "already applied" says nothing about what
                        # is in flash. Force the current values in, otherwise
                        # setting the active profile as onboard would silently
                        # do nothing.
                        kb.commit_to_flash()
                        packets = 0
                    else:
                        # persist=True already writes with ERASE_AND_WRITE, so a
                        # follow-up commit would burn a second flash cycle.
                        packets = p.execute(kb, persist=True)
                    s.active_profile = name
                    return packets

                packets = s.submit(commit, timeout=90)
                state = write_onboard(s.profile_dir, name)
                return {"packets": packets, "onboard": state}

            if path == "/api/focus":
                if d.get("clear"):
                    s.focus = None
                    s.focus_trace = []
                else:
                    s.focus = (int(d["row"]), int(d["col"]))
                    s.focus_trace = []
                return {"focus": s.focus}

            # app binding rules

            if path == "/api/settings/save":
                s_path = Path(s.profile_dir).parent / "settings.json"
                st = AppSettings.load(s_path)
                if "check_updates" in d:
                    st.check_updates = bool(d["check_updates"])
                if "start_at_login" in d:
                    st.start_at_login = set_start_at_login(bool(d["start_at_login"]))
                st.save(s_path)
                return {"check_updates": st.check_updates,
                        "start_at_login": get_start_at_login()}

            # profile export / import

            if path == "/api/profile/export":
                # One profile, or every profile in a single bundle. Both are
                # plain JSON: a profile already is JSON, so a bundle is just a
                # list of them with a marker so import can tell them apart.
                which = d.get("name")
                if which:
                    name = _safe_profile_name(which)
                    src = s.profile_dir / f"{name}.json"
                    if not src.exists():
                        raise ValueError(f"profile '{name}' does not exist")
                    payload = json.loads(src.read_text(encoding="utf-8-sig"))
                    default = f"{name}.json"
                else:
                    payload = {
                        "kind": "mad68-profile-bundle",
                        "exported": _dt.datetime.now().astimezone().isoformat(
                            timespec="seconds"),
                        "profiles": [
                            json.loads(p.read_text(encoding="utf-8-sig"))
                            for p in sorted(s.profile_dir.glob("*.json"))
                        ],
                    }
                    default = "mad68-profiles.json"
                dest = browse_save_json(
                    "Export profile" if which else "Export all profiles", default)
                if not dest:
                    return {"saved": None}
                Path(dest).write_text(json.dumps(payload, indent=2),
                                      encoding="utf-8")
                return {"saved": dest,
                        "count": 1 if which else len(payload["profiles"])}

            if path == "/api/profile/import":
                src = d.get("path") or browse_open_json("Import profile")
                if not src:
                    return {"imported": []}
                data = json.loads(Path(src).read_text(encoding="utf-8-sig"))
                docs = (data.get("profiles", [])
                        if isinstance(data, dict)
                        and data.get("kind") == "mad68-profile-bundle"
                        else [data])
                imported, skipped = [], []
                for doc in docs:
                    try:
                        name = _safe_profile_name(doc.get("name"))
                    except Exception:
                        skipped.append(str(doc.get("name"))[:40])
                        continue
                    # Validate before writing: an unparsable profile on disk
                    # would break the sidebar for every later load.
                    try:
                        Profile.from_json(doc)
                    except Exception as exc:
                        skipped.append(f"{name} ({exc})")
                        continue
                    dest = s.profile_dir / f"{name}.json"
                    if dest.exists() and not d.get("overwrite"):
                        # Never silently replace someone's tuning.
                        stem, n = name, 2
                        while (s.profile_dir / f"{stem} ({n}).json").exists():
                            n += 1
                        name = f"{stem} ({n})"
                        doc["name"] = name
                        dest = s.profile_dir / f"{name}.json"
                    doc.pop("triggers", None)
                    doc.pop("trigger_exe", None)
                    doc.pop("trigger_title", None)
                    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                    imported.append(name)
                return {"imported": imported, "skipped": skipped}

            if path == "/api/profile/default":
                # Which profile the switcher falls back to when no rule matches.
                # switcher.json holds a single default_profile string, so
                # exclusivity is structural: marking one profile necessarily
                # unmarks whichever held it before. Nothing has to hunt down the
                # previous default and clear a flag on it.
                name = _safe_profile_name(d.get("name"))
                dest = s.profile_dir / f"{name}.json"
                if not dest.exists():
                    raise ValueError(f"profile '{name}' does not exist")
                want = bool(d.get("default", True))

                cfg_path = Path(s.profile_dir).parent / "switcher.json"
                try:
                    cfg = (SwitcherConfig.load(cfg_path) if cfg_path.exists()
                           else SwitcherConfig())
                except Exception:
                    cfg = SwitcherConfig()
                previous = cfg.default_profile

                if want:
                    cfg.default_profile = name
                    # The default profile is the fallback for "no app matched",
                    # so an app trigger on it would be contradictory, and the
                    # UI greys those fields out, which would leave a rule the
                    # user can see firing but cannot edit. Drop it.
                    doc = json.loads(dest.read_text(encoding="utf-8-sig"))
                    doc["triggers"] = []
                    doc.pop("trigger_exe", None)
                    doc.pop("trigger_title", None)
                    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                    cfg.rules = [r for r in cfg.rules if r.profile != name]
                elif cfg.default_profile == name:
                    cfg.default_profile = ""

                cfg.save(cfg_path)
                return {"default_profile": cfg.default_profile,
                        "previous": previous}

            if path == "/api/rules/save":
                cfg_path = Path(s.profile_dir).parent / "switcher.json"
                cfg = SwitcherConfig.load(cfg_path) if cfg_path.exists() else SwitcherConfig()
                cfg.default_profile = str(d.get("default_profile", cfg.default_profile))
                cfg.enabled = bool(d.get("enabled", cfg.enabled))
                rules = []
                for r in d.get("rules", []):
                    prof = r.get("profile")
                    if not prof:
                        continue
                    rules.append(Rule(profile=prof, exe=r.get("exe") or None,
                                      title_contains=r.get("title_contains") or None))
                cfg.rules = rules
                cfg.save(cfg_path)
                return {"rules": len(rules)}

            # macros

            if path == "/api/macro/set":
                index = int(d["index"])
                steps = [_macro_step(x) for x in (d.get("steps") or [])]
                return s.submit(lambda kb: kb.write_macro_slot(index, steps), timeout=40)

            if path == "/api/macro/clear":
                index = int(d["index"])
                return s.submit(lambda kb: kb.write_macro_slot(index, []), timeout=40)

            raise KeyError(path)

    return Handler


def _reconcile_rules(profile_dir: Path) -> int:
    """Rebuild switcher.json's rules from the profiles' own trigger fields.

    A profile owns "which app launches me"; switcher.json is a projection of
    that for the tray to read. The two are written by different code paths and
    had drifted: a profile carried trigger_exe while switcher.json listed no
    rule for it, so the binding existed in the UI and did nothing at runtime.

    default_profile and the timing knobs are left alone, they are not
    derived from any profile.
    """
    cfg_path = Path(profile_dir).parent / "switcher.json"
    try:
        cfg = SwitcherConfig.load(cfg_path) if cfg_path.exists() else SwitcherConfig()
    except Exception:
        return 0

    wanted: list[Rule] = []
    for path in sorted(Path(profile_dir).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        wanted += [Rule(profile=path.stem, exe=t["exe"] or None,
                        title_contains=t["title"] or None)
                   for t in _doc_triggers(doc)]

    before = [(r.profile, r.exe, r.title_contains) for r in cfg.rules]
    after = [(r.profile, r.exe, r.title_contains) for r in wanted]
    if before == after:
        return 0
    cfg.rules = wanted
    cfg.save(cfg_path)
    return len(wanted)


def serve(profile_dir: Path, host: str = "127.0.0.1", port: int = 8787,
          interval: float = 0.03) -> tuple[ThreadingHTTPServer, Sampler]:
    if ensure_default_profile(profile_dir):
        print(f"created the default profile in {profile_dir}")
    if _reconcile_rules(profile_dir):
        print("switcher.json rules rebuilt from the profiles' app triggers")
    sampler = Sampler(profile_dir, interval=interval)
    sampler.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(sampler))
    return httpd, sampler


# Shared styling. Rich black surface, red accent, semi-transparent panels.
#
# The heatmap ramp is a validated single-hue sequential scale (monotone
# lightness, even steps, 28 degrees of hue spread) checked with the dataviz
# validator against this exact surface. Zero-travel keys are left unfilled so
# the dimmest step is only ever used for real travel, and a table view plus
# numeric labels provide relief for the low-contrast end.

CSS = r"""
:root {
  color-scheme: dark;
  --bg: #0d0d0f;
  --bg-deep: #050506;
  --panel: rgba(24,24,27,0.72);
  --panel-solid: #17171a;
  --panel-brd: rgba(255,255,255,0.08);
  --panel-brd-hi: rgba(255,255,255,0.16);
  --ink: #f4f4f5;
  --ink-2: #a9a9b2;
  --ink-3: #74747e;
  --accent: #ec3339;
  --accent-hi: #ff5c45;
  --accent-dim: rgba(236,51,57,0.16);
  --good: #0ca30c;
  --warn: #fab219;
  --grid: rgba(255,255,255,0.07);
  --field: rgba(0,0,0,0.30);
  /* One spacing step and one control height everywhere, so rows line up and
     panels share a rhythm instead of each picking its own numbers. */
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px; --s6: 32px;
  --ctl-h: 34px;
  --radius: 10px;
  --radius-lg: 14px;
  --r1:#4a1418; --r2:#68171e; --r3:#871a24; --r4:#a91d28; --r5:#ca222d;
  --r6:#e63036; --r7:#f75c45; --r8:#ff8a68; --r9:#ffb894;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  /* A single restrained wash. Two saturated red radials read as a gaming skin;
     the tool should sit back and let the keyboard be the loud thing. */
  background:
    radial-gradient(1200px 640px at 12% -12%, rgba(236,51,57,0.07), transparent 62%),
    var(--bg);
  background-attachment: fixed;
  color: var(--ink);
  font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent-hi); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1240px; margin: 0 auto; padding: var(--s5) 22px 60px; }
.panel {
  background: var(--panel);
  backdrop-filter: blur(16px) saturate(1.1);
  -webkit-backdrop-filter: blur(16px) saturate(1.1);
  border: 1px solid var(--panel-brd);
  border-radius: var(--radius-lg);
  padding: var(--s5);
  margin-bottom: var(--s4);
}
h1 { font-size: 19px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }
h2 { font-size: 11px; margin: 0 0 var(--s3); font-weight: 600; letter-spacing: .07em;
     text-transform: uppercase; color: var(--ink-3); }
h3 { font-size: 14px; font-weight: 600; letter-spacing: 0; }
.sub { color: var(--ink-3); font-size: 12.5px; line-height: 1.55; }
/* Explanatory text sits under a control and should not run the full width of a
   wide panel -- long measures are what made the panels feel like documentation. */
.sub { max-width: 62ch; }
.brand { display: flex; align-items: center; gap: 11px; margin-bottom: 18px; }
/* Same wordmark as the sidebar, a little larger for the landing page. Width
   drives it; the aspect ratio supplies the height. */
.brandmark { width: 100%; max-width: 230px; height: auto; object-fit: contain;
             display: block; }
.dot-brand { width: 10px; height: 10px; border-radius: 50%; background: var(--accent);
             box-shadow: 0 0 14px 3px rgba(236,51,57,0.65); flex: none; }
.row { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
/* Every settings row leads with a label cell. Pinning one width for all of them
   keeps the controls in a single column down the page: they were each carrying
   their own inline width (60/64/70/80/90/96/110px), so every row started at a
   slightly different x and the panels looked ragged. `min-width` is deliberate
   -- it wins over the inline `width` without having to edit each call site. */
.row > b:first-child, .row > .lab { flex: 0 0 auto; min-width: 104px; }
.row > .lab { color: var(--ink-2); }
.spacer { margin-left: auto; }
/* Buttons, inputs and selects share one height so anything sitting on the same
   row has a common baseline. Mixed intrinsic heights were what made rows look
   subtly misaligned even when the markup was right. */
button, .btn {
  font: inherit; height: var(--ctl-h); padding: 0 14px; border-radius: var(--radius);
  cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; white-space: nowrap;
  border: 1px solid var(--panel-brd); background: rgba(255,255,255,0.05);
  color: var(--ink); transition: background .13s ease, border-color .13s ease;
}
button:hover:not(:disabled), .btn:hover { background: rgba(255,255,255,0.09);
                                          border-color: var(--panel-brd-hi); }
button:active:not(:disabled) { transform: translateY(1px); }
button.primary, .btn.primary {
  background: var(--accent); border-color: transparent; color: #fff; font-weight: 600;
}
button.primary:hover, .btn.primary:hover { background: var(--accent-hi); }
button[aria-pressed="true"] { background: var(--accent-dim); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .4; cursor: not-allowed; }
input[type=number], input[type=text], select {
  font: inherit; height: var(--ctl-h); background: var(--field); color: var(--ink);
  border: 1px solid var(--panel-brd); border-radius: var(--radius); padding: 0 10px;
  transition: border-color .13s ease, background .13s ease;
}
input[type=number]:hover, input[type=text]:hover, select:hover { border-color: var(--panel-brd-hi); }
/* A visible focus ring on every interactive control -- keyboard users had no
   indication of where they were. */
button:focus-visible, input:focus-visible, select:focus-visible, .btn:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
input[type=number]:focus, input[type=text]:focus, select:focus {
  border-color: var(--accent); background: rgba(0,0,0,0.45);
}
input[type=range] { accent-color: var(--accent); height: var(--ctl-h); }
input[type=color] { width: 44px; height: var(--ctl-h); padding: 2px; background: transparent;
                    border: 1px solid var(--panel-brd); border-radius: var(--radius);
                    cursor: pointer; }
label.f { display: inline-flex; align-items: center; gap: var(--s2); color: var(--ink-2);
          font-size: 12.5px; }
/* Numeric readouts beside a slider: tabular figures stop the row twitching as
   the value changes width. */
.row > span[id$="val"] { font-variant-numeric: tabular-nums; color: var(--ink-2); }
.tiles { display: flex; flex-wrap: wrap; gap: 11px; margin-bottom: 16px; }
.tile { background: var(--panel); backdrop-filter: blur(14px);
        -webkit-backdrop-filter: blur(14px);
        border: 1px solid var(--panel-brd); border-radius: 12px;
        padding: 12px 16px; min-width: 126px; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-3); }
.tile .v { font-size: 23px; font-weight: 620; margin-top: 3px; }
.tile .v small { font-size: 12px; font-weight: 400; color: var(--ink-3); }
.scroller { overflow-x: auto; }
.kbd { display: grid; grid-template-columns: repeat(15, 1fr); gap: 5px; min-width: 760px; }
.cell {
  position: relative; aspect-ratio: 1/1; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 10.5px; font-weight: 500; cursor: pointer;
  border: 1px solid var(--grid); background: rgba(255,255,255,0.025);
  color: var(--ink-2); transition: background-color 60ms linear, color 60ms linear;
  overflow: hidden; text-align: center; line-height: 1.1;
}
.cell.empty { border-style: dashed; opacity: .3; cursor: default; }
.cell.uncal { border-color: rgba(236,51,57,0.55); }
.cell.hot { color: #fff; }
.cell.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
.legend { display: flex; align-items: center; gap: 9px; margin-top: 13px;
          font-size: 12px; color: var(--ink-3); flex-wrap: wrap; }
.ramp { display: flex; height: 9px; border-radius: 3px; overflow: hidden; width: 190px; }
.ramp i { flex: 1; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 16px; }
.tabs button { border-radius: 9px 9px 0 0; border-bottom-color: transparent; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 5px 9px; border-bottom: 1px solid var(--grid); font-size: 13px; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
th { color: var(--ink-3); font-weight: 500; font-size: 11px; text-transform: uppercase;
     letter-spacing: .04em; }
.err { color: var(--accent-hi); }
.ok { color: var(--good); }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11px;
        border: 1px solid var(--panel-brd); color: var(--ink-2); }
.pill.on { background: var(--accent-dim); border-color: var(--accent); color: #fff; }
#tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity 80ms;
       background: var(--panel-solid); border: 1px solid var(--panel-brd);
       border-radius: 9px; padding: 9px 11px; font-size: 12px;
       box-shadow: 0 10px 30px rgba(0,0,0,.6); z-index: 50; white-space: nowrap; }
#tip b { font-weight: 600; }
#tip div { color: var(--ink-3); }
#toast { position: fixed; right: 18px; bottom: 18px; z-index: 60;
         display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
.toast { background: var(--panel-solid); border: 1px solid var(--panel-brd);
         border-left: 3px solid var(--accent); border-radius: 9px;
         padding: 9px 13px; font-size: 13px; box-shadow: 0 10px 30px rgba(0,0,0,.6); }
.toast.bad { border-left-color: var(--accent-hi); color: var(--accent-hi); }
.hidden { display: none !important; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }

/* ---- live travel gauge -------------------------------------------------
   A vertical bar per Wootility: travel fills downward from the top, the fill
   is dim while the key is below its actuation point and bright once the
   firmware would count it as pressed. Marker lines show where actuation and
   the rapid-trigger release sit. */
.gauge-wrap { display: flex; gap: 18px; align-items: flex-start; }
.gauge {
  position: relative; width: 62px; height: 260px; flex: none;
  border: 1px solid var(--panel-brd); border-radius: 9px;
  background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(0,0,0,0.35));
  overflow: hidden;
}
.gauge .fill {
  position: absolute; left: 0; right: 0; top: 0;
  background: var(--r3);
  transition: height 40ms linear, background-color 60ms linear;
}
.gauge.act .fill { background: var(--accent-hi); box-shadow: 0 0 22px rgba(255,92,69,.55); }
.gauge .mark { position: absolute; left: 0; right: 0; height: 0;
               border-top: 1px dashed rgba(255,255,255,0.5); }
.gauge .mark span { position: absolute; right: 3px; top: 2px; font-size: 9px;
                    color: var(--ink-2); text-shadow: 0 1px 3px #000; }
.gauge .mark.rel { border-top-style: dotted; border-top-color: rgba(255,255,255,0.34); }
.gauge .readout { position: absolute; left: 0; right: 0; bottom: 4px;
                  text-align: center; font-size: 11px; color: var(--ink-2);
                  font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px #000; }
.state-pill { font-size: 11px; padding: 3px 10px; border-radius: 999px;
              border: 1px solid var(--panel-brd); color: var(--ink-2); }
.state-pill.on { background: var(--accent); border-color: transparent; color: #fff; }

/* ---- stock-style two-pane layout, toggles and slider rows --------------- */
.split { display: grid; grid-template-columns: 300px 1fr; gap: 0; }
@media (max-width: 860px) { .split { grid-template-columns: 1fr; } }
.split > .left { padding-right: 22px; border-right: 1px solid var(--panel-brd); }
.split > .right { padding-left: 24px; }
.split h3 { margin: 0 0 12px; font-size: 16px; font-weight: 600; }
.split .blurb { color: var(--ink-3); font-size: 12.5px; line-height: 1.6; margin-bottom: 20px; }

.switch { position: relative; display: inline-block; width: 46px; height: 25px; flex: none; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch span {
  position: absolute; inset: 0; cursor: pointer; border-radius: 999px;
  background: #4a4a4e; transition: background .16s;
}
.switch span::before {
  content: ""; position: absolute; height: 19px; width: 19px; left: 3px; top: 3px;
  background: #fff; border-radius: 50%; transition: transform .16s;
}
.switch input:checked + span { background: var(--accent); }
.switch input:checked + span::before { transform: translateX(21px); }

.setting { margin-bottom: 26px; }
.setting .name { font-size: 14px; font-weight: 600; margin-bottom: 5px; }
.setting .desc { color: var(--ink-3); font-size: 12.5px; line-height: 1.55; margin-bottom: 12px; }
.sliderrow { display: flex; align-items: center; gap: 16px; }
.sliderrow input[type=range] { flex: 1; min-width: 140px; }
.stepper { display: flex; align-items: center; gap: 0; border: 1px solid var(--panel-brd);
           border-radius: 8px; overflow: hidden; background: rgba(0,0,0,0.3); }
.stepper button { border: 0; border-radius: 0; background: transparent; padding: 7px 11px;
                  color: var(--ink-2); font-size: 15px; line-height: 1; }
.stepper button:hover { background: rgba(255,255,255,0.08); }
.stepper input { border: 0; border-radius: 0; background: transparent; width: 62px;
                 text-align: right; font-variant-numeric: tabular-nums; }
.stepper .unit { padding-right: 9px; color: var(--ink-3); font-size: 12px; }
.dim { opacity: .4; pointer-events: none; }

.toggles { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
           gap: 22px 34px; }
.toggle-item { display: flex; gap: 16px; align-items: flex-start;
               justify-content: space-between; }
.toggle-item .t-name { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
.toggle-item .t-desc { color: var(--ink-3); font-size: 12.5px; line-height: 1.5; }
.danger { border-color: rgba(236,51,57,0.5) !important; color: var(--accent-hi) !important; }
.danger:hover { background: rgba(236,51,57,0.14) !important; }
"""

BOARD_CSS = r"""
   board is exactly the same size on every tab and every layer regardless of how
   long the legends are. */
:root { --u: 56px; --kgap: calc(var(--u) * 5 / 56); }
.board {
  background: linear-gradient(180deg, #efefee, #c6c6c3);
  border-radius: calc(var(--u) * 16 / 56);
  padding: calc(var(--u) * 16 / 56) calc(var(--u) * 16 / 56) 0 calc(var(--u) * 26 / 56);
  position: relative;
  width: calc(var(--u) * 16 + var(--u) * 42 / 56); flex: none;
  box-shadow: 0 14px 46px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.7);
}
/* The buckle on the top-left of the real case. It is part of the case, not a
   separate tab bolted to it: a short section of the left edge that sticks out a
   little further than the rest, with a recessed slot cut into it. So it carries
   the board's own gradient, is rounded on its outer edge only, and casts no
   shadow on the side that meets the board -- otherwise the seam shows and it
   reads as a floating bar. */
.board::before {
  content: ""; position: absolute;
  left: calc(var(--u) * -12 / 56); top: calc(var(--u) * 18 / 56);
  width: calc(var(--u) * 24 / 56); height: calc(var(--u) * 86 / 56);
  border-radius: calc(var(--u) * 9 / 56) 0 0 calc(var(--u) * 9 / 56);
  background: linear-gradient(180deg, #ededec, #c9c9c6);
  box-shadow: -3px 2px 7px rgba(0,0,0,.30),
              inset 1px 0 0 rgba(255,255,255,.85),
              inset 0 1px 0 rgba(255,255,255,.7);
}
.board::after {
  content: ""; position: absolute;
  left: calc(var(--u) * -5 / 56); top: calc(var(--u) * 32 / 56);
  width: calc(var(--u) * 9 / 56); height: calc(var(--u) * 58 / 56);
  border-radius: calc(var(--u) * 5 / 56);
  background: linear-gradient(180deg, #26262a, #141416);
  box-shadow: inset 0 2px 4px rgba(0,0,0,.9), 0 1px 0 rgba(255,255,255,.55);
}
/* The keyless lip along the bottom of the case: darker than the deck so it
   reads as the moulded edge, bleeding out to the board's sides so it spans the
   full width. The engraving lives on it, right-aligned with matching top,
   right and bottom padding.
   NOTE: this deliberately does not use `.brand` -- that class is the sidebar
   logo and carries `display:flex`, which silently defeats text-align. */
.bezel {
  margin: calc(var(--u) * 13 / 56) calc(var(--u) * -16 / 56) 0
          calc(var(--u) * -26 / 56);
  padding: calc(var(--u) * 10 / 56) calc(var(--u) * 20 / 56)
           calc(var(--u) * 10 / 56) 0;
  text-align: right;
  background: linear-gradient(180deg, rgba(0,0,0,0.09), rgba(0,0,0,0.27));
  border-bottom-left-radius: calc(var(--u) * 16 / 56);
  border-bottom-right-radius: calc(var(--u) * 16 / 56);
  border-top: 1px solid rgba(0,0,0,0.10);
}
.kbrand {
  display: inline-block;
  font-size: calc(var(--u) * 19 / 56); font-weight: 800; letter-spacing: .34em;
  transform: scaleY(1.25);
  font-family: "Segoe UI", system-ui, sans-serif;
  color: rgba(74,74,72,0.55);
  text-shadow: 0 1px 0 rgba(255,255,255,0.7), 0 -1px 1px rgba(0,0,0,0.38);
  pointer-events: none; user-select: none;
}
.krow { display: flex; gap: var(--kgap); margin-bottom: var(--kgap); }
.kcap {
  /* Never shrink. Flex items default to flex-shrink: 1, so a container
     narrower than the row squashes every cap and the layout stops
     matching the real keyboard. */
  flex: 0 0 auto;
  background: linear-gradient(180deg, #232327, #131315);
  border-radius: calc(var(--u) * 6 / 56); color: #f2f2f3;
  font-size: calc(var(--u) * 11.5 / 56); line-height: 1.15; display: flex; align-items: center;
  justify-content: center; text-align: center;
  padding: calc(var(--u) * 4 / 56) calc(var(--u) * 3 / 56); cursor: pointer;
  border: 1px solid rgba(255,255,255,0.09); position: relative;
  height: calc(var(--u) * 50 / 56); user-select: none; overflow: hidden;
  box-shadow: 0 2px 0 rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.06);
}
.kcap:hover { border-color: rgba(255,255,255,0.34); }
.kcap.sel { border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(236,51,57,.55) inset, 0 2px 0 rgba(0,0,0,.5); }
.kcap.dim { opacity: .35; }
.kcap .lbl { pointer-events: none; color: #f2f2f3; }
.kcap .tl, .kcap .bl, .kcap .br {
  position: absolute; font-size: 9px; color: #6fd66f; pointer-events: none;
  font-variant-numeric: tabular-nums;
}
.kcap .tl { top: 3px; left: 4px; color: #ff7b7b; }
.kcap .bl { bottom: 3px; left: 4px; }
.kcap .br { bottom: 3px; right: 4px; }
.kcap .bolt { position: absolute; top: 3px; right: 4px; font-size: 9px; color: #ffd166; }
.kcap .swatch { position: absolute; inset: auto 4px 4px 4px; height: 4px; border-radius: 2px;
                box-shadow: 0 0 5px currentColor; }
.kcap.perf .lbl { font-size: 10.5px; }
"""

# The physical 65 percent layout, as [row, col, width in units]. Shared so the
# home page and the configurator draw the same keyboard rather than two
# different approximations of it.
BOARD_JS = r"""
const LAYOUT=[
 [[0,0,1],[0,1,1],[0,2,1],[0,3,1],[0,4,1],[0,5,1],[0,6,1],[0,7,1],[0,8,1],[0,9,1],
  [0,10,1],[0,11,1],[0,12,1],[0,13,2],[0,14,1]],
 [[1,0,1.5],[1,1,1],[1,2,1],[1,3,1],[1,4,1],[1,5,1],[1,6,1],[1,7,1],[1,8,1],[1,9,1],
  [1,10,1],[1,11,1],[1,12,1],[1,13,1.5],[1,14,1]],
 [[2,0,1.75],[2,1,1],[2,2,1],[2,3,1],[2,4,1],[2,5,1],[2,6,1],[2,7,1],[2,8,1],[2,9,1],
  [2,10,1],[2,11,1],[2,13,2.25],[2,14,1]],
 [[3,0,2.25],[3,2,1],[3,3,1],[3,4,1],[3,5,1],[3,6,1],[3,7,1],[3,8,1],[3,9,1],[3,10,1],
  [3,11,1],[3,12,1.75],[3,13,1],[3,14,1]],
 [[4,0,1.25],[4,1,1.25],[4,2,1.25],[4,6,6.25],[4,9,1],[4,10,1],[4,11,1],[4,12,1],
  [4,13,1],[4,14,1]],
];
const LEGEND_STATIC={
 "0,0":"Esc","0,13":"Backspace","0,14":"Del",
 "1,0":"Tab","1,13":"\\","1,14":"PgUp",
 "2,0":"Caps","2,13":"Enter","2,14":"PgDn",
 "3,0":"Shift","3,12":"Shift","3,13":"↑","3,14":"End",
 "4,0":"Ctrl","4,1":"Win","4,2":"Alt","4,6":"","4,9":"Alt","4,10":"Fn",
 "4,11":"Ctrl","4,12":"←","4,13":"↓","4,14":"→",
};
const ROW1="1234567890-=", ROW2="QWERTYUIOP[]", ROW3="ASDFGHJKL;'", ROW4="ZXCVBNM,./";
function staticLegend(r,c){
  const k=`${r},${c}`;
  if(k in LEGEND_STATIC)return LEGEND_STATIC[k];
  if(r===0&&c>=1&&c<=12)return ROW1[c-1]||"";
  if(r===1&&c>=1&&c<=12)return ROW2[c-1]||"";
  if(r===2&&c>=1&&c<=11)return ROW3[c-1]||"";
  if(r===3&&c>=2&&c<=11)return ROW4[c-2]||"";
  return "";
}
/* A non-interactive board, same markup and metrics as the configurator's. */
function staticBoardHTML(){
  const rows=LAYOUT.map(row=>{
    const caps=row.map(([r,c,w])=>
      `<div class="kcap" style="width:calc(var(--u) * ${w} - 5px)"
        ><span class="lbl">${staticLegend(r,c)}</span></div>`).join("");
    return `<div class="krow">${caps}</div>`;
  }).join("");
  return `<div class="board">${rows}
    <div class="bezel"><span class="kbrand">MADLIONS</span></div></div>`;
}
"""

RAMP_JS = """
const RAMP = ["--r1","--r2","--r3","--r4","--r5","--r6","--r7","--r8","--r9"];
function rampVar(t){ if(t<=0) return null;
  return RAMP[Math.min(RAMP.length-1, Math.max(0, Math.round(t*(RAMP.length-1))))]; }
"""

PAGE_HOME = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMAD</title>
<link rel="icon" href="/icon.ico">
<style>""" + CSS + BOARD_CSS + r"""
/* The board is drawn at its real size and scaled as one piece, so every
   proportion stays identical to the configurator rather than being re-tuned
   here and drifting out of step.

   On this page it is decoration, not a control: it sits behind the device panel
   and runs off the bottom right corner, faded back so the text on top stays the
   thing you read. */
.devpanel { position: relative; overflow: hidden; min-height: 300px; }
.boarddeco {
  /* The board derives every dimension from --u, so a smaller unit gives a
     true miniature. No transform, no clipping tricks, nothing to drift. */
  --u: 30px;
  position: absolute; right: calc(var(--u) * -3); bottom: calc(var(--u) * -2.2);
  width: max-content;
  opacity: .16; filter: saturate(.3);
  pointer-events: none; user-select: none; z-index: 0;
}
@media (max-width: 900px) { .boarddeco { --u: 22px; } }
/* Everything else in the panel sits above it. */
.devbody { position: relative; z-index: 1; text-align: center; }

.hero { text-align: center; padding: 10px 0 26px; }
.hero h1 { font-size: 30px; font-weight: 650; letter-spacing: -0.02em;
           margin: 0 0 10px; }
.hero .tag { color: var(--ink-2); font-size: 15.5px; line-height: 1.6;
             max-width: 56ch; margin: 0 auto; }

.status { display: inline-flex; align-items: center; gap: 10px;
          border: 1px solid var(--panel-brd); border-radius: 999px;
          padding: 7px 16px 7px 13px; background: rgba(0,0,0,.28); }
.status .led { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.led.on { background: var(--good); box-shadow: 0 0 12px 2px rgba(12,163,12,.6); }
.led.off { background: var(--accent); box-shadow: 0 0 12px 2px rgba(236,51,57,.55); }

.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
@media (max-width: 900px) { .cards { grid-template-columns: 1fr; } }
.card { background: rgba(255,255,255,.03); border: 1px solid var(--panel-brd);
        border-radius: var(--radius-lg); padding: 18px; }
.card h3 { margin: 0 0 6px; }
.card p { margin: 0; color: var(--ink-3); font-size: 13px; line-height: 1.6; }

/* Two tight columns centred as a block. Stretching them across the panel put
   the labels and their values at opposite edges, which read as unrelated. */
dl { display: grid; grid-template-columns: max-content max-content;
     gap: 7px 20px; margin: 18px auto 0; width: max-content;
     font-size: 13px; text-align: left; }
dt { color: var(--ink-3); text-align: right; }
dd { margin: 0; font-variant-numeric: tabular-nums; }

/* Ko-fi's own brand red. Their blue would read as an ordinary link, and the
   app's accent red is darker and more saturated, so this stays distinct from
   the primary buttons around it. */
.kofi { background: #FF5E5B; border-color: transparent; color: #fff;
        font-weight: 600; font-size: 15px; height: auto;
        padding: 14px 26px; border-radius: 12px; gap: 10px;
        box-shadow: 0 6px 20px rgba(255,94,91,.28); }
.kofi:hover { background: #ff7370; border-color: transparent;
              box-shadow: 0 8px 24px rgba(255,94,91,.38); }
.kofi svg { width: 22px; height: 22px; flex: none; }
.footer { text-align: center; color: var(--ink-3); font-size: 12.5px;
          margin-top: 26px; }
</style></head>
<body><div class="wrap">
  <div class="brand" style="justify-content:center">
    <img class="brandmark" src="/logo.png" alt="OpenMAD"
         onerror="this.replaceWith(Object.assign(document.createElement('h1'),
                                                 {textContent:'OpenMAD'}))"></div>

  <div class="hero">
    <h1>Your keyboard, on your machine.</h1>
    <p class="tag">A local driver for the MAD68 HE. Everything the official
      web tool does, plus named profiles and automatic switching when you
      change game.</p>
  </div>

  <div class="panel devpanel">
    <div class="boarddeco" id="board" aria-hidden="true"></div>
    <div class="devbody">
      <div class="status"><span class="led off" id="led"></span>
        <strong id="dev-title">Looking for your keyboard…</strong></div>
      <div class="sub" id="dev-note" style="margin:10px auto 0">
        Scanning USB HID for vendor 0x373b.</div>
      <dl id="dev-info"></dl>
      <div class="row" style="justify-content:center; margin-top:22px">
        <button class="primary" id="open" disabled>Open the configurator</button>
        <button id="recheck">Re-scan</button>
      </div>
    </div>
  </div>

  <div class="cards">
    <div class="card">
      <h3>Profiles that follow you</h3>
      <p>Bind a profile to one or more games. It loads when they take focus and
         falls back to your default when you close them.</p>
    </div>
    <div class="card">
      <h3>Kind to your keyboard</h3>
      <p>Switching applies settings without writing to flash, so it costs none
         of the memory's limited write cycles. Only you decide when to save.</p>
    </div>
    <div class="card">
      <h3>Nothing leaves your PC</h3>
      <p>No account, no cloud, no telemetry. Profiles are plain files you can
         read, back up and share.</p>
    </div>
  </div>

  <div class="panel" style="text-align:center; margin-top:16px">
    <h3 style="margin:0 0 8px">Free and open source</h3>
    <div class="sub" style="margin:0 auto 16px">
      Built by reverse-engineering the official configurator, and given away.
      If it saved you some trouble, you can buy me a coffee.</div>
    <a class="btn kofi" href="https://ko-fi.com/acciaw" target="_blank" rel="noopener">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path fill="currentColor" d="M4.2 4.6h13.1c.55 0 1 .45 1 1v1.15h1.3
          a3.85 3.85 0 0 1 0 7.7h-1.62A6.6 6.6 0 0 1 11.7 19H8.45
          A5.25 5.25 0 0 1 3.2 13.75V5.6c0-.55.45-1 1-1Zm14.1 4.45v3.85h1.3
          a1.93 1.93 0 0 0 0-3.85h-1.3Z"/>
        <path fill="#fff" d="M7.65 8.2c1-.9 2.4-.6 3.02.22.62-.82 2.02-1.12 3.02-.22
          .92.85.72 2.3-.18 3.2-.72.72-1.9 1.6-2.6 2.1a.4.4 0 0 1-.5 0
          c-.7-.5-1.88-1.38-2.6-2.1-.9-.9-1.08-2.35-.16-3.2Z"/>
      </svg>
      Support me on Ko-fi</a>
  </div>

  <div class="footer">
    OpenMAD is not affiliated with or endorsed by the manufacturer.</div>
</div>
<div id="toast"></div>
<script>
""" + BOARD_JS + r"""
document.getElementById("board").innerHTML = staticBoardHTML();
function toast(msg, bad) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : ""); t.textContent = msg;
  document.getElementById("toast").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
async function check() {
  const led = document.getElementById("led");
  const title = document.getElementById("dev-title");
  const note = document.getElementById("dev-note");
  const info = document.getElementById("dev-info");
  const open = document.getElementById("open");
  try {
    const d = await (await fetch("/api/device")).json();
    if (d.present) {
      led.className = "led on";
      title.textContent = "MAD68 HE connected";
      note.textContent = "Raw HID config interface found on usage page 0xff60.";
      info.innerHTML =
        `<dt>Product</dt><dd>${d.product ?? "-"}</dd>` +
        `<dt>Manufacturer</dt><dd>${d.manufacturer ?? "-"}</dd>` +
        `<dt>Serial</dt><dd>${d.serial ?? "-"}</dd>` +
        `<dt>VID:PID</dt><dd>${d.vendor_id}:${d.product_id}</dd>`;
      open.disabled = false;
    } else {
      led.className = "led off";
      title.textContent = d.bootloader ? "Keyboard is in bootloader mode"
                                       : "No MAD68 HE detected";
      note.textContent = d.bootloader
        ? "Unplug and replug it to return to normal operation."
        : (d.reason || "Connect the keyboard directly, not through a KVM.");
      info.innerHTML = `<dt>Looking for</dt><dd>${d.vendor_id}:${d.product_id}</dd>`;
      open.disabled = true;
    }
  } catch (e) {
    led.className = "led off";
    title.textContent = "Driver not reachable";
    note.textContent = "Is OpenMAD still running?";
    open.disabled = true;
  }
}
document.getElementById("open").onclick = () => location.href = "/hud";
document.getElementById("recheck").onclick = () => { toast("Re-scanning…"); check(); };
check(); setInterval(check, 3000);
</script>
</body></html>
"""

PAGE_HUD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMAD</title>
<link rel="icon" href="/icon.ico">
<style>""" + CSS + r"""
/* ---- app shell -------------------------------------------------------- */
/* 268px, not 240: a profile that is both the fallback and the one saved to the
   keyboard carries two badges, and at 240 the pair left the name about 26px to
   render in, which hid it almost completely. */
.app { display: grid; grid-template-columns: 268px 1fr; min-height: 100vh; }
.sidebar {
  background: rgba(12,12,14,0.72); backdrop-filter: blur(14px);
  border-right: 1px solid var(--panel-brd); padding: 20px 16px 16px;
  display: flex; flex-direction: column; gap: 8px;
  /* Pinned to the viewport: the profile list scrolls inside it rather than
     stretching the sidebar past the bottom of the page. */
  height: 100vh; position: sticky; top: 0; overflow: hidden;
}
/* assets/logo.png is a wide wordmark (roughly 4.7:1), so it is sized by width
   and lets its own aspect ratio set the height -- forcing it into a square box
   would squash it. If the file is missing the markup swaps in a text wordmark,
   which is why no fixed height is set here. */
.logo { display: flex; align-items: center; margin-bottom: 22px;
        min-height: 44px; border-radius: var(--radius);
        transition: opacity .13s ease; }
.logo:hover { opacity: .78; }
.logo img { width: 100%; max-width: 190px; height: auto; object-fit: contain;
            display: block; }
.logo b { font-size: 19px; letter-spacing: .06em; }
/* Settings sits at the foot of the sidebar, pushed there rather than placed
   with a fixed offset so it stays put whatever the profile list does. */
.settingsbtn { width: 100%; flex: none; justify-content: flex-start;
               gap: 9px; background: var(--accent); border-color: transparent;
               color: #fff; font-weight: 600; }
.settingsbtn:hover:not(:disabled) { background: var(--accent-hi);
                                    border-color: transparent; }
.settingsbtn .gear { font-size: 15px; line-height: 1; }
.conn { display: flex; align-items: center; gap: 8px; flex: none;
        font-size: 12.5px; color: var(--ink-3); padding: 12px 3px 10px; }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; flex: none;
             background: var(--ink-3); }
.conn.on .dot { background: var(--good);
                box-shadow: 0 0 9px 1px rgba(12,163,12,.55); }
.navbtn {
  display: block; width: 100%; height: auto; text-align: left; padding: 9px 12px;
  border-radius: var(--radius); border: 1px solid transparent; background: transparent;
  color: var(--ink-2); font: inherit; cursor: pointer; text-decoration: none;
  box-sizing: border-box;
}
.navbtn:hover:not(:disabled) { background: rgba(255,255,255,0.05); color: var(--ink); }
.navbtn.on { border-color: var(--accent); color: #fff; }
.navbtn:disabled { opacity: .32; cursor: not-allowed; }
.sect { display: flex; align-items: center; gap: var(--s2); margin: var(--s5) 0 var(--s2);
        color: var(--ink-3); font-size: 11px; text-transform: uppercase;
        letter-spacing: .07em; font-weight: 600; }
.sect .spacer { margin-left: auto; }
.iconbtn { width: 26px; height: 26px; padding: 0; display: grid; place-items: center;
           border-radius: 7px; font-size: 15px; line-height: 1; }
.iconbtn svg { width: 15px; height: 15px; fill: none; stroke: currentColor;
               stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.plist { display: flex; flex-direction: column; gap: 2px;
         flex: 1 1 auto; min-height: 0; overflow-y: auto; }
/* A row rather than a button, because it holds its own options button and a
   button cannot legally contain another one. */
.prow {
  text-align: left; border: 1px solid transparent; background: transparent;
  border-radius: var(--radius); padding: 7px 6px 7px 12px; color: var(--ink-2);
  cursor: pointer; display: flex; align-items: center; gap: 6px;
}
.prow:hover { background: rgba(255,255,255,0.05); color: var(--ink); }
/* The selected profile carries an accent rail, so which one is open is legible
   at a glance rather than from a faint background wash. */
.prow.on { background: rgba(255,255,255,0.07); color: #fff;
           box-shadow: inset 2px 0 0 var(--accent); }
/* A floor, so the name is always legible. Without one the badges won every
   fight for space and the name was ellipsised down to nothing. */
.pname { flex: 1 1 auto; min-width: 5em; overflow: hidden;
         text-overflow: ellipsis; white-space: nowrap; }
/* Shrinkable, unlike the name: if a row genuinely runs out of room the badge
   gives way first, since it is the shorter and more guessable of the two. */
.plist .badge { flex: 0 1 auto; min-width: 0; overflow: hidden;
                font-size: 9px; font-weight: 600; white-space: nowrap;
                text-transform: uppercase;
                color: var(--accent-hi); background: var(--accent-dim);
                border-radius: 5px; padding: 2px 5px; }
.prow.on .badge { color: #fff; }
/* Visible on hover and on the open profile, so the row stays quiet otherwise
   but the affordance is always reachable where you are looking. */
.pmenu {
  flex: none; width: 22px; height: 22px; padding: 0; border: 0;
  background: transparent; color: inherit; border-radius: 6px;
  display: grid; place-items: center; cursor: pointer;
  font-size: 15px; line-height: 1; opacity: 0;
}
.prow:hover .pmenu, .prow.on .pmenu { opacity: .7; }
.pmenu:hover { background: rgba(255,255,255,.12); opacity: 1; }

/* Context menu. Anchored to whatever opened it and clamped to the viewport. */
.ctxmenu {
  position: fixed; z-index: 120; min-width: 168px; padding: 5px;
  background: var(--panel-solid); border: 1px solid var(--panel-brd);
  border-radius: var(--radius); box-shadow: 0 16px 40px rgba(0,0,0,.6);
  display: none;
}
.ctxmenu.open { display: block; }
.ctxmenu button {
  display: flex; width: 100%; height: 32px; justify-content: flex-start;
  border: 0; background: transparent; border-radius: 7px; padding: 0 10px;
  color: var(--ink-2); cursor: pointer; font: inherit; font-size: 13px;
}
.ctxmenu button:hover { background: rgba(255,255,255,.08); color: var(--ink); }
.ctxmenu button.danger { color: var(--accent-hi); }
.ctxmenu button.danger:hover { background: rgba(236,51,57,.15); }

main { min-width: 0; }
/* Tabs read as tabs: an underline on the active one instead of a full red
   fill, which was competing with the primary buttons for attention. */
.toptabs { display: flex; border-bottom: 1px solid var(--panel-brd);
           background: rgba(12,12,14,0.5); }
.toptabs button {
  flex: 1; height: auto; border: 0; border-radius: 0; background: transparent;
  padding: 15px 8px; color: var(--ink-3); font: inherit; font-size: 13px;
  cursor: pointer; box-shadow: inset 0 -2px 0 transparent;
  transition: color .13s ease, box-shadow .13s ease, background .13s ease;
}
.toptabs button:hover { background: rgba(255,255,255,0.03); color: var(--ink-2); }
.toptabs button.on { color: #fff; font-weight: 600;
                     box-shadow: inset 0 -2px 0 var(--accent);
                     background: rgba(255,255,255,0.04); }
.page { padding: var(--s5) 26px 60px; }

/* ---- realistic keyboard ------------------------------------------------
   Sizing is absolute, not flexible: one key unit is a fixed pixel width, so the
""" + BOARD_CSS + r"""
.selbar { display: flex; flex-direction: column; gap: 8px; }
.selbar button { text-align: left; white-space: nowrap; }
.boardwrap { display: flex; gap: 14px; align-items: flex-start; justify-content: center; }

.notice { background: rgba(236,51,57,0.14); border: 1px solid rgba(236,51,57,0.4);
          color: #ffb4a8; border-radius: 999px; padding: 7px 16px; font-size: 12.5px;
          display: inline-flex; gap: 9px; align-items: center; margin: 0 auto 14px;
          justify-content: center; }
.center { display: flex; justify-content: center; }
.layerbar { display: flex; gap: 8px; align-items: center; justify-content: center;
            margin: 16px 0 10px; color: var(--ink-3); font-size: 13px; }
/* The Advanced Key row carries five long labels. With `width: fit-content` and
   no wrapping the pill grew past the page and the last tab -- Sappy Tnappy --
   sat off the right edge where it could not be clicked at all. It has to be
   able to wrap and never exceed its container. */
.subtabs { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center;
           margin: 14px auto 18px; background: rgba(0,0,0,0.32); padding: 5px;
           border-radius: 22px; width: fit-content; max-width: 100%; }
.subtabs button { border: 0; background: transparent; border-radius: 999px;
                  padding: 8px 18px; color: var(--ink-2); font: inherit;
                  cursor: pointer; white-space: nowrap; }
.subtabs button:hover:not(:disabled):not(.on) { background: rgba(255,255,255,.07); }
.subtabs button.on { background: var(--accent); color: #fff; font-weight: 600; }
.subtabs button:disabled { opacity: .38; cursor: not-allowed; }

/* keycode picker: three fixed-width blocks, main / navigation / numpad */
:root { --pu: 44px; }
.picker { background: rgba(0,0,0,0.28); border-radius: 12px; padding: 18px;
          overflow-x: auto; }
.pkblocks { display: flex; gap: 22px; align-items: flex-start;
            width: max-content; margin: 0 auto; }
.pkrow { display: flex; gap: 5px; margin-bottom: 5px; }
.pk { background: rgba(255,255,255,0.05); border: 1px solid var(--panel-brd);
      border-radius: 6px; color: #9fb6d6; font-size: 11px;
      cursor: pointer; text-align: center; height: 39px;
      display: flex; align-items: center; justify-content: center;
      user-select: none; }
.pk:hover { background: var(--accent); color: #fff; border-color: transparent; }
.numpad { display: grid; grid-template-columns: repeat(4, var(--pu));
          grid-auto-rows: 39px; gap: 5px; }
.numpad .pk { width: auto; height: auto; }
/* System / Macro / Light Effect sets: a plain wrapping row of square buttons */
.iconrow { display: flex; flex-wrap: wrap; gap: 9px; justify-content: center; }
.pk.icon { width: 46px; height: 46px; font-size: 17px; color: var(--ink-2); }
.pk.icon:hover { color: #fff; }

/* lighting */
.modes { display: grid; grid-template-columns: repeat(5, 1fr); gap: 9px; }
.modes button { padding: 9px 6px; font-size: 12px; white-space: nowrap;
                overflow: hidden; text-overflow: ellipsis; }
.modes button.on { background: var(--accent); border-color: transparent; color: #fff; }
.swatches { display: flex; flex-wrap: wrap; gap: 8px; }
.swatches button { width: 34px; height: 34px; border-radius: 8px; padding: 0;
                   border: 1px solid rgba(255,255,255,0.18); }
.swatchbtn { width: 30px; height: 30px; border-radius: 7px; padding: 0;
             border: 1px solid rgba(255,255,255,0.22); }
.warn { color: var(--accent-hi); font-size: 12px; line-height: 1.5; }
/* Keymap notice at the top of Change Key Setting. Key bindings live on the
   keyboard and are never applied by an automatic profile switch, so this is
   where the user finds out whether what they are editing is what the board is
   currently running. */
.kmnote { display: flex; align-items: flex-start; gap: var(--s3);
          border: 1px solid var(--panel-brd); border-radius: var(--radius-lg);
          padding: 13px 16px; margin-bottom: var(--s4); font-size: 12.5px;
          line-height: 1.55; }
.kmnote.ok { background: rgba(12,163,12,.09); border-color: rgba(12,163,12,.30);
             color: var(--ink-2); }
.kmnote.warn2 { background: rgba(250,178,25,.10);
                border-color: rgba(250,178,25,.35); color: var(--ink-2); }
.kmnote .kmicon { flex: none; font-size: 15px; line-height: 1.3; }
.kmnote .kmbody { flex: 1 1 auto; min-width: 0; }
.kmnote b { color: var(--ink); }
.kmnote .row { margin-top: 10px; }

/* Shown across every tab when the keyboard reports a protocol version this
   driver has not been checked against. Sits above the tab strip rather than
   inside one tab, since a wire format mismatch is not specific to whatever
   the user happens to be looking at. */
.verbanner { display: flex; align-items: center; gap: var(--s3);
             background: rgba(250,178,25,.12); border-bottom: 1px solid rgba(250,178,25,.35);
             color: var(--warn); font-size: 12.5px; padding: 10px 26px; }
.verbanner b { color: inherit; }
/* "Make this the default profile" -- set apart from the app fields it disables,
   so the relationship between the two reads at a glance. */
.defbox { margin-top: 18px; padding: 14px 16px; border-radius: 10px;
          background: rgba(255,255,255,0.035);
          border: 1px solid var(--panel-brd); }
.defbox.on { border-color: rgba(236,51,57,.55);
             background: rgba(236,51,57,.09); }
.defbox label { gap: 11px; cursor: pointer; }
.defbox input[type=checkbox] { margin-top: 2px; width: 16px; height: 16px;
                               accent-color: var(--accent); cursor: pointer; }

/* modal */
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.62); z-index: 80;
         display: grid; place-items: center; }
.modal .box { background: #1b1b1f; border: 1px solid var(--panel-brd);
              border-radius: 12px; padding: 24px; width: min(920px, 92vw);
              box-shadow: 0 20px 60px rgba(0,0,0,.7); }

/* ---- advanced-key editors --------------------------------------------- */
.edhead { display: flex; align-items: center; gap: 10px; padding: 14px 22px;
          border-bottom: 1px solid var(--panel-brd); }
.edhead b { font-size: 15px; }
.edbody { display: grid; gap: 34px; padding: 24px 22px; }
.edtype { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.edright { border-left: 1px solid var(--panel-brd); padding-left: 28px; }
.keypair { display: flex; gap: 26px; margin-top: 22px; }
.klabel { font-size: 12.5px; color: var(--ink-2); text-align: center; margin-bottom: 8px; }
.keybox {
  width: 52px; height: 52px; border-radius: 8px; cursor: pointer;
  background: rgba(255,255,255,0.07); border: 1px solid var(--panel-brd);
  display: grid; place-items: center; font-size: 12px; color: var(--ink);
  margin: 0 auto;
}
.keybox:hover { border-color: rgba(255,255,255,0.35); }
.keybox.picking { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(236,51,57,.4); }
.radiorow { display: flex; align-items: center; gap: 12px; padding: 13px 16px;
            border: 1px solid var(--panel-brd); border-radius: 9px;
            margin-bottom: 9px; cursor: pointer; background: rgba(255,255,255,0.03); }
.radiorow:hover { background: rgba(255,255,255,0.06); }
.radiorow.on { border-color: var(--accent); }
.radio { width: 16px; height: 16px; border-radius: 50%; flex: none;
         border: 2px solid var(--ink-3); }
.radio.on { border-color: var(--accent); background:
            radial-gradient(circle, var(--accent) 0 45%, transparent 46%); }
.qmark { width: 17px; height: 17px; border-radius: 50%; border: 1px solid var(--ink-3);
         color: var(--ink-3); font-size: 11px; display: grid; place-items: center;
         cursor: help; flex: none; }
.addbtn { border: 1px dashed var(--panel-brd); background: transparent;
          padding: 16px 28px; border-radius: 10px; color: var(--ink-2); width: 100%;
          text-align: center; }
.addbtn:hover { border-color: var(--accent); color: #fff; }
.dksgrid { display: grid; grid-template-columns: 90px repeat(4, 74px);
           gap: 12px; justify-content: center; align-items: center; margin-top: 18px; }
.dkszone { text-align: center; }
.dkszone .ic { font-size: 21px; color: var(--ink-2); }
.dkszone .mm { font-size: 11px; color: var(--ink-3); margin-top: 2px; }
.dkscell { height: 42px; display: grid; place-items: center; cursor: pointer;
           border-radius: 8px; border: 1px solid transparent; color: var(--ink-3);
           font-size: 17px; }
.dkscell:hover { background: rgba(255,255,255,0.06); }
.dkscell.on { color: var(--accent-hi); }
.dksslot { display: grid; place-items: center; }

/* macro */
.mgrid { display: grid; grid-template-columns: 250px 1fr 300px; gap: 16px; }
.mstep { display: flex; align-items: center; gap: 0; background: rgba(255,255,255,0.04);
         border: 1px solid var(--panel-brd); border-radius: 9px; margin-bottom: 8px;
         overflow: hidden; }
.mstep .grip { padding: 12px 10px; color: var(--ink-3); background: rgba(255,255,255,0.05); }
.mstep .dir { padding: 0 12px; font-size: 15px; }
.mstep .kc { background: rgba(255,255,255,0.08); border-radius: 6px; padding: 5px 12px;
             font-size: 12px; }
.mstep .ms { margin-left: auto; padding: 0 12px; font-variant-numeric: tabular-nums; }
.mstep .del { padding: 0 12px; color: var(--accent-hi); cursor: pointer; }

/* Below this the fixed-size keyboard cannot fit beside the sidebar. The
   breakpoint is the sidebar (240px) plus the board (938px) plus page padding. */
#toonarrow { display: none; }
@media (max-width: 1230px) {
  .app { display: none; }
  #toonarrow {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: var(--s5); text-align: center;
    min-height: 100vh; padding: var(--s5); color: var(--ink-2);
  }
  #toonarrow img { width: 96px; height: auto; max-height: 96px;
                   object-fit: contain; }
  #toonarrow p { margin: 0; font-size: 15px; line-height: 1.6; max-width: 30ch; }
}
</style></head>
<body>
<div class="app">
  <aside class="sidebar">
    <a class="logo" href="/" title="Back to the start page">
      <img src="/logo.png" alt="OpenMAD"
           onerror="this.replaceWith(Object.assign(document.createElement('b'),
                                                   {textContent:'OpenMAD'}))"></a>
    <button class="navbtn" id="nav-equipment">Equipment</button>
    <!-- Macros are stored on the keyboard, not in a profile: all 16 slots are
         shared by every profile. Keeping them out of the per-profile tabs is
         the only honest place for them. -->
    <button class="navbtn" id="nav-macros">Macros</button>
    <a class="navbtn" href="https://hub.fgg.com.cn/" target="_blank" rel="noopener"
       title="Flashing is not implemented here. Opens the official configurator.">
      Firmware &#8599;</a>
    <div class="sect">Device Profiles
      <span class="spacer"></span>
      <button class="iconbtn" id="p-add" title="New profile">+</button>
      <button class="iconbtn" id="p-import" title="Import a profile from a file">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 2.5 8.2 7.8"/>
          <path d="M8.2 3.8v4h4"/>
          <path d="M12 9.6v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5.1a1 1 0 0 1 1-1h3"/></svg>
      </button>
      <button class="iconbtn" id="p-onboard" title="Save selected profile to the keyboard">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.2v7.2"/>
          <path d="M4.8 6.4 8 9.6l3.2-3.2"/>
          <path d="M2.8 13.2h10.4"/></svg>
      </button>
    </div>
    <div class="plist" id="plist"></div>
    <div class="conn" id="conn"><span class="dot"></span>
      <span id="conn-text">connecting…</span></div>
    <button class="settingsbtn" id="open-settings">
      <span class="gear">&#9881;</span> Settings</button>
  </aside>

  <main>
    <div id="verbanner"></div>
    <nav class="toptabs" id="toptabs"></nav>
    <div class="page" id="page"></div>
  </main>
</div>
<!-- The keyboard is drawn at a fixed pixel size so it stays identical across
     tabs and layers, which means below a certain width the layout cannot work.
     Saying so beats letting the board overlap the panels. -->
<div id="toonarrow">
  <img src="/icon.png" alt="" onerror="this.style.display='none'">
  <p>Your window isn't leaving enough room for the app!<br>
     Please resize it or use a different monitor.</p>
</div>
<div id="tip"></div><div id="toast"></div><div id="modal"></div>
<div class="ctxmenu" id="ctxmenu"></div>

<script>
""" + RAMP_JS + r"""
const $ = s => document.querySelector(s);
let latest=null, config=null, profiles=[], editing=null, editDoc=null;
let sel=new Set(), layer=0, tab="Change Key Setting", subtab="Travel";
let pickTab="Keyboard Key";
let akSub="Rappy Snappy (RS)", pickTarget=null, gaugeKey=null;
/* Name of the profile the switcher falls back to, or "" for none. */
let defaultProfile="";
/* Blank trigger rows added with "+" but not yet filled in. Not persisted --
   an empty trigger is not a rule. */
let apExtra=0;
/* A profile's trigger apps, reading either schema: `triggers` is the list,
   `trigger_exe`/`trigger_title` the single binding it replaced. */
function docTriggers(doc){
  if(!doc)return[];
  if(Array.isArray(doc.triggers))
    return doc.triggers.map(t=>({exe:t.exe||"",title:t.title||""}))
                       .filter(t=>t.exe||t.title);
  const e=(doc.trigger_exe||"").trim(), t=(doc.trigger_title||"").trim();
  return (e||t)?[{exe:e,title:t}]:[];
}
/* The two keys being picked for a new RS/SOCD pair.
   These live at module scope on purpose. They used to be locals inside
   wirePage(), and `pickTarget` used to be a closure over them -- so every
   redraw built a fresh scope while `pickTarget` still pointed at the old one.
   A key clicked after any redraw wrote to the dead copy and updated a detached
   DOM node, so the label never changed and Add still said "pick both keys
   first". Since patchProfile() redraws, that happened constantly, which is what
   made these buttons work only until the first refresh. */
let akK1=null, akK2=null;
function showAkPicks(){
  const a=document.querySelector("#ak-k1"), b=document.querySelector("#ak-k2");
  if(a)a.textContent=akK1===null?"—":(keyLabel(akK1)||"?");
  if(b)b.textContent=akK2===null?"—":(keyLabel(akK2)||"?");
}

/* Per-profile tabs. "Macro Setting" is deliberately absent: the 16 macro
   slots live on the keyboard and are shared by every profile, so presenting
   them here would imply they are part of the profile. They are reached from
   the sidebar instead. */
const TABS=["Change Key Setting","Lighting Setting","Performance","Advanced Key",
            "App Trigger","Other Settings"];
/* The vendor's own configurator, which is where firmware flashing lives. The
   sidebar's Firmware link points at the same place from static markup. */
const FIRMWARE_URL="https://hub.fgg.com.cn/";
/* Which sidebar section is showing: the profile editor, or the shared
   device-wide macro editor. */
let view="equipment";

/* physical 65% layout: [row, col, width-in-units]. Rows total 16u.
   Enter absorbs the empty r2c12, LShift absorbs r3c1, Space absorbs
   r4 c3-c5 and c7-c8 -- which is why those matrix slots read as empty. */
const LAYOUT=[
 [[0,0,1],[0,1,1],[0,2,1],[0,3,1],[0,4,1],[0,5,1],[0,6,1],[0,7,1],[0,8,1],[0,9,1],
  [0,10,1],[0,11,1],[0,12,1],[0,13,2],[0,14,1]],
 [[1,0,1.5],[1,1,1],[1,2,1],[1,3,1],[1,4,1],[1,5,1],[1,6,1],[1,7,1],[1,8,1],[1,9,1],
  [1,10,1],[1,11,1],[1,12,1],[1,13,1.5],[1,14,1]],
 [[2,0,1.75],[2,1,1],[2,2,1],[2,3,1],[2,4,1],[2,5,1],[2,6,1],[2,7,1],[2,8,1],[2,9,1],
  [2,10,1],[2,11,1],[2,13,2.25],[2,14,1]],
 [[3,0,2.25],[3,2,1],[3,3,1],[3,4,1],[3,5,1],[3,6,1],[3,7,1],[3,8,1],[3,9,1],[3,10,1],
  [3,11,1],[3,12,1.75],[3,13,1],[3,14,1]],
 [[4,0,1.25],[4,1,1.25],[4,2,1.25],[4,6,6.25],[4,9,1],[4,10,1],[4,11,1],[4,12,1],
  [4,13,1],[4,14,1]],
];
const idx=(r,c)=>r*15+c;

/* keycode names, mirroring src/mad68/keycodes.py */
const KC={};
for(let i=0;i<26;i++)KC[0x04+i]=String.fromCharCode(65+i);
"1234567890".split("").forEach((c,i)=>KC[0x1e+i]=c);
for(let i=0;i<12;i++)KC[0x3a+i]="F"+(i+1);
Object.assign(KC,{0x00:"",0x01:"",0x28:"Enter",0x29:"Esc",0x2a:"Back Space",0x2b:"Tab",
 0x2c:"Space",0x2d:"-",0x2e:"=",0x2f:"[",0x30:"]",0x31:"\\",0x33:";",0x34:"'",0x35:"`",
 0x36:",",0x37:".",0x38:"/",0x39:"Caps Lock",0x46:"Print Screen",0x47:"Scroll Lock",
 0x48:"Pause",0x49:"Insert",0x4a:"Home",0x4b:"Page Up",0x4c:"Delete",0x4d:"End",
 0x4e:"Page Down",0x4f:"Right",0x50:"Left",0x51:"Down",0x52:"Up",0x53:"Num Lock",
 0x54:"/",0x55:"*",0x56:"-",0x57:"+",0x58:"Enter",0x65:"Menu",
 0xe0:"Left Ctrl",0xe1:"Left Shift",0xe2:"Left Alt",0xe3:"Left Win",
 0xe4:"Right Ctrl",0xe5:"Right Shift",0xe6:"Right Alt",0xe7:"Right Win"});
for(let i=0;i<10;i++)KC[0x59+i]=String(i===9?0:i+1);
const KC_REV={};
for(const[k,v]of Object.entries(KC))if(v)KC_REV[v.toLowerCase()]=+k;
/* System / media keys. These are the HID consumer and system usages QMK exposes
   in its basic keycode range, which is what `Tu.system = 0x7c` and the vendor's
   System tab are driving. [glyph, tooltip, keycode] */
const SYSTEM_KEYS=[
  ["⌸","Calculator",0xB2],      ["✉","Mail",0xB1],
  ["♫","Media Player",0xAF],    ["⌸","My Computer",0xB3],
  ["⌕","Search",0xB4],          ["⌂","Browser Home",0xB5],
  ["⇦","Browser Back",0xB6],    ["⇨","Browser Forward",0xB7],
  ["⊙","Browser Stop",0xB8],    ["↻","Refresh",0xB9],
  ["⚑","Favourites",0xBA],      ["☼","Brightness +",0xBD],
  ["☀","Brightness −",0xBE],
  ["⏮","Previous Track",0xAC],  ["⏭","Next Track",0xAB],
  ["ὐ7","Mute",0xA8],           ["▶","Volume +",0xA9],
  ["◀","Volume −",0xAA],   ["⏹","Stop",0xAD],
  ["⏯","Play / Pause",0xAE],    ["⏩","Fast Forward",0xBB],
  ["⏪","Rewind",0xBC],          ["⏏","Eject",0xB0],
  ["⏻","Power",0xA5],           ["☾","Sleep",0xA6],
  ["☆","Wake",0xA7],
];

/* RGB controls. `Tu.light = 0x78` puts these in the 0x78xx range, matching
   QMK's lighting keycodes. The nine here are the ones the stock UI offers. */
const RGB_KEYS=[
  ["⏻","Turn RGB Lighting On or Off",0x7800],
  ["⇦","Next RGB Mode",0x7801],
  ["⇨","Previous RGB Mode",0x7802],
  ["⊕","Increase Color Hue",0x7803],
  ["⊖","Decrease Hue",0x7804],
  ["☼","Increase Brightness",0x7807],
  ["☀","Decrease Brightness",0x7808],
  ["≫","Increase RGB effect speed",0x7809],
  ["≪","Decrease the RGB effect speed",0x780A],
];

const hexToObj=h=>({r:parseInt(h.slice(1,3),16),g:parseInt(h.slice(3,5),16),
                    b:parseInt(h.slice(5,7),16)});

function kcName(kc){
  if(kc>=0x5220&&kc<=0x523f)return "Fn"+(kc-0x5220);
  if(kc>=0x7700&&kc<=0x770f)return "M"+(kc-0x7700+1);
  const rgb=RGB_KEYS.find(x=>x[2]===kc); if(rgb)return rgb[0];
  const sys=SYSTEM_KEYS.find(x=>x[2]===kc); if(sys)return sys[0];
  return KC[kc]!==undefined?KC[kc]:("0x"+Number(kc).toString(16));
}
function parseKey(s){
  if(!s)return null; s=s.trim();
  if(/^0x[0-9a-f]+$/i.test(s))return parseInt(s,16);
  if(/^\d+$/.test(s))return parseInt(s,10);
  const v=KC_REV[s.toLowerCase()]; return v===undefined?null:v;
}

/* Lighting effects, taken verbatim from the stock web app's own table
   (ConfigPage-8DLKiHxJ.js, list `Bt`, which "MAD68 HE" maps to). These are not
   guesses any more: the numbers, the names and the per-effect capabilities are
   the vendor's.

   An earlier list here was wrong in two ways that made every effect look
   broken. From Spectrum Cycle onwards the labels were shifted off their
   numbers, so picking a name applied a different effect; and 36 and 39 were
   missing entirely because the probe that built the list only swept 0..31.
   Number 10 was in the list and does not exist.

   `c` -- the effect uses the colour. The rainbow and spectrum effects generate
          their own and ignore what we send.
   `s` -- the effect animates and takes a speed (1..255).
   Effects with neither are static and take brightness only, which is why
   "Rainbow static decreasing left and right" is a still image: it is meant to
   be. */
const EFFECTS=[
 [0,"Close",              0,0],
 [1,"Customization",      1,0],
 [2,"Monochromatic Constant Brightness", 1,0],
 [3,"Focus",              1,0],
 [4,"Rainbow static decreasing up and down",    1,0],
 [5,"Rainbow static decreasing left and right", 1,0],
 [6,"Monochromatic Breathing", 1,1],
 [7,"Ribbon fluttering 1", 1,1],
 [8,"Ribbon fluttering 2", 1,1],
 [9,"Rotating Belt 1",    1,1],
 [11,"Rotating Belt 2",   1,1],
 [13,"Spectrum Cycle",    0,1],
 [19,"Colorful Swirls 1", 0,1],
 [23,"Colorful Swirls 2", 0,1],
 [25,"Christmas",         0,0],
 [26,"Spectrum Breathing",1,1],
 [28,"Spectrum Floating", 1,1],
 [30,"Digital Rain",      0,0],
 [31,"Monochromatic Lighting Up", 1,1],
 [36,"Crucifix",          1,1],
 [39,"Fingertip Rainbow", 0,1]];
const EFFECT_BY_N={}; EFFECTS.forEach(e=>{EFFECT_BY_N[e[0]]=e;});
/* The firmware ignores any brightness above this and keeps the previous value,
   so the slider must not offer more. The stock app carries the same ceiling as
   a per-model override: `{"MAD 68 RGB": 0xd2}`. */
const BRIGHT_MAX=210;
/* The vendor's table gives speed as 1..255. Zero is out of range and freezes an
   animation on its first frame -- which is exactly what a broken effect looks
   like, so never send it. Profiles written before this control existed all
   carry speed 0 and fall back to this. */
const DEFAULT_SPEED=128;
/* A profile that has never had a colour picked carries r=g=b=0, and black is
   not something the LEDs can show. For the effects that take a colour -- the
   two Rainbow static modes among them -- sending black collapses the whole
   pattern to one dead hue, which is why a rainbow stopped looking like a
   rainbow until a colour had been set once. Nothing in the packet distinguishes
   "never set" from "deliberately black", and deliberate black is
   indistinguishable from off anyway, so all-zero is read as unset and given a
   colour the board can actually render. */
const DEFAULT_RGB=[255,255,255];
function liRGB(li){
  const r=+(li.r||0), g=+(li.g||0), b=+(li.b||0);
  return (r||g||b)?[r,g,b]:DEFAULT_RGB.slice();
}
function liHex(li){
  return "#"+liRGB(li).map(v=>Number(v).toString(16).padStart(2,"0")).join("");
}
const SWATCHES=["#e91e8c","#ef4d92","#f4695f","#f57c1f","#f0a51e","#f5d020",
 "#a8d030","#22a04a","#2f7fe0","#7c4dd0"];

/* Short names for the advanced-key modes, shown on each bound row.
   This was referenced by the RS/SOCD list and never actually defined, so
   rendering threw as soon as a profile had one bound -- and because the throw
   happened inside draw(), wirePage() never ran and every button in the section
   was dead. With an empty list the map body never executed, so it looked
   intermittent rather than broken. */
const AK_LABEL={
  RS:"Rappy Snappy", OKS:"OKS", NONE:"—",
  SOCD:"Last input priority", SOCD_KEY1:"Key 1 priority",
  SOCD_KEY2:"Key 2 priority", SOCD_BALANCE:"Neutral"};

function toast(m,bad){const t=document.createElement("div");
  t.className="toast"+(bad?" bad":"");t.textContent=m;$("#toast").appendChild(t);
  setTimeout(()=>t.remove(),4200);}
async function post(p,b){const r=await fetch(p,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
  const j=await r.json(); if(!r.ok||j.error){toast(j.error||"request failed",true);
  throw new Error(j.error);} return j.result;}
async function loadConfig(){try{config=await(await fetch("/api/config")).json();}
  catch(e){toast("could not read the keyboard",true);}
  drawVersionBanner();}
/* Key bindings live on the keyboard, not in a profile, and switching profiles
   deliberately does not rewrite them. This says which profiles currently share
   what is on the board, and offers the one explicit way to change it. */
async function drawKeymapNote(){
  const el=$("#kmbanner");
  if(!el||!editing)return;
  let s2;
  try{
    s2=await(await fetch(`/api/keymap-status?name=${encodeURIComponent(editing)}`)).json();
  }catch(e){return;}
  if(!s2||s2.error)return;
  const p=s2.profile||{};
  const others=(s2.same||[]).filter(n=>n!==editing);
  const shared=others.length
    ? `Also matches <b>${others.join("</b>, <b>")}</b>, so switching between
       them leaves your key bindings alone.`
    : `No other profile currently matches it.`;

  if(!p.has_keymap){
    el.innerHTML=`<div class="kmnote ok"><span class="kmicon">&#9432;</span>
      <div class="kmbody">Editing the keyboard's key bindings directly.
        <b>${editing}</b> has no saved keymap of its own, so it will never
        change them when it becomes active. ${shared}
        <div class="row"><button id="km-capture">Save current keys to this profile</button></div>
      </div></div>`;
  }else if(p.matches){
    el.innerHTML=`<div class="kmnote ok"><span class="kmicon">&#10003;</span>
      <div class="kmbody">The keyboard is running <b>${editing}</b>'s key
        bindings. ${shared}</div></div>`;
  }else{
    el.innerHTML=`<div class="kmnote warn2"><span class="kmicon">&#9888;</span>
      <div class="kmbody"><b>${editing}</b> has different key bindings from the
        ones currently on the keyboard
        (<b>${p.keys_differ}</b> key${p.keys_differ===1?"":"s"} differ).
        Key bindings are stored on the keyboard itself and are never changed by
        an automatic profile switch, because unlike every other setting they
        cannot be applied without writing to its permanent memory.
        <div class="row">
          <button class="primary" id="km-apply">Apply this profile's keys</button>
          <button id="km-capture">Use the keyboard's current keys instead</button>
        </div>
      </div></div>`;
  }
  const ap=el.querySelector("#km-apply"), cap=el.querySelector("#km-capture");
  if(ap)ap.onclick=async()=>{
    ap.disabled=true;
    try{
      const r=await post("/api/profile/keymap/apply",{name:editing});
      toast(r.packets?`key bindings applied`:"already matching");
      await loadConfig();draw();
    }catch(e){ap.disabled=false;}
  };
  if(cap)cap.onclick=async()=>{
    cap.disabled=true;
    try{
      await post("/api/profile/keymap/capture",{name:editing});
      toast(`saved the keyboard's keys to ${editing}`);
      draw();
    }catch(e){cap.disabled=false;}
  };
}

function drawVersionBanner(){
  const el=$("#verbanner");
  if(!el)return;
  // config.protocol_version_known is false only when the keyboard's own
  // reported protocol version differs from the one this driver was verified
  // against. That means firmware changed, not that anything here is broken --
  // but every packet layout in the app was reverse engineered from one
  // specific firmware, so it is worth saying plainly rather than silently
  // hoping nothing moved.
  if(!config||config.protocol_version_known!==false){el.innerHTML="";return;}
  el.innerHTML=`<span>&#9888;</span>
    <span>This keyboard reports protocol version <b>${config.protocol_version}</b>,
      different from the version this driver was built and tested against.
      A firmware update likely changed the keyboard's data format, so settings
      here may read or apply incorrectly. Back up first, and consider using
      the official configurator until this is confirmed to work.</span>`;
}
async function refreshProfiles(){try{
  profiles=(await(await fetch("/api/profiles")).json()).profiles||[];}catch(e){}
  // The fallback profile lives in switcher.json, not in any profile file, so
  // exactly one can hold it at a time.
  try{defaultProfile=((await(await fetch("/api/rules")).json())
    .default_profile)||"";}catch(e){}}

/* ---- selection helpers ------------------------------------------------- */
const populated=()=>LAYOUT.flat().map(([r,c])=>idx(r,c));
function selectAll(){sel=new Set(populated());draw();}
function selectNone(){sel=new Set();draw();}
function selectInvert(){const all=populated();
  sel=new Set(all.filter(i=>!sel.has(i)));draw();}
function firstSel(){return sel.size?[...sel][0]:null;}

/* ---- keyboard rendering ------------------------------------------------ */
/* Dual legends, as the stock app prints them on the caps. */
const LEGEND={0x1e:"!1",0x1f:"@2",0x20:"#3",0x21:"$4",0x22:"%5",0x23:"^6",0x24:"&7",
 0x25:"*8",0x26:"(9",0x27:")0",0x2d:"_-",0x2e:"+=",0x2f:"{[",0x30:"}]",0x31:"|\\",
 0x33:":;",0x34:"\"'",0x35:"~`",0x36:"<,",0x37:">.",0x38:"?/"};
function keyLabel(i){
  const L=config&&config.layers&&config.layers[layer];
  if(!L)return "";
  const r=Math.floor(i/15),c=i%15;
  const cell=L[r]&&L[r][c];
  if(!cell)return "";
  return LEGEND[cell.kc]||kcName(cell.kc);
}
function boardHTML(mode){
  const rows=LAYOUT.map(row=>{
    const caps=row.map(([r,c,w])=>{
      const i=idx(r,c);
      let inner=`<span class="lbl">${keyLabel(i)}</span>`;
      if(mode==="perf"&&editDoc){
        const key=`${r},${c}`;
        const a=(editDoc.actuation?.overrides_mm||{})[key]??editDoc.actuation?.default_mm??1.5;
        const rt=(editDoc.rapid_trigger?.overrides||{})[key]??editDoc.rapid_trigger?.default??{};
        inner=`<span class="tl">${Number(a).toFixed(2)}</span>`+
              (rt.enabled?`<span class="bolt">&#9889;</span>`:``)+
              `<span class="lbl">${keyLabel(i)}</span>`+
              `<span class="bl">${Number(rt.press_mm??0).toFixed(2)}</span>`+
              `<span class="br">${Number(rt.release_mm??0).toFixed(2)}</span>`;
      }else if(mode==="light"&&editDoc){
        const col=(editDoc.key_colors||[])[i]||[0,0,0];
        inner=`<span class="lbl">${keyLabel(i)}</span>`+
          `<span class="swatch" style="background:rgb(${col[0]},${col[1]},${col[2]})"></span>`;
      }else if(mode==="adv"&&editDoc){
        const bound=(editDoc.advanced_keys||[]).some(a=>a.mode&&a.mode!=="NONE"&&
          ((a.key1_row===r&&a.key1_col===c)||(a.key2_row===r&&a.key2_col===c)));
        inner=`<span class="lbl">${keyLabel(i)}</span>`+
          (bound?`<span class="bolt">&#9679;</span>`:``);
      }
      // Absolute width per key unit, so the board never resizes with content.
      const px=`calc(var(--u) * ${w} - var(--kgap))`;
      return `<div class="kcap${sel.has(i)?" sel":""}${mode==="perf"?" perf":""}"
        data-i="${i}" style="width:${px}">${inner}</div>`;
    }).join("");
    return `<div class="krow">${caps}</div>`;
  }).join("");
  return `<div class="board"><div>${rows}</div>
    <div class="bezel"><span class="kbrand">MADLIONS</span></div></div>`;
}
function selbarHTML(){
  return `<div class="selbar">
    <button id="s-all">&#10003;&nbsp; Select all</button>
    <button id="s-inv">&#8646;&nbsp; Select invert</button>
    <button id="s-none">&#8856;&nbsp; Deselect all</button></div>`;
}
function wireBoard(root,mode){
  root.querySelectorAll(".kcap").forEach(el=>el.onclick=e=>{
    const i=+el.dataset.i;
    if(akEdit&&akSlot&&(akEdit.kind==="RS"||akEdit.kind==="SOCD")){
      const r=Math.floor(i/15),c=i%15;
      const kc=(config.layers?.[0]?.[r]?.[c]||{}).kc;
      if(akSlot==="key1"){akEdit.k1r=r;akEdit.k1c=c;akEdit.key1=kc;}
      else{akEdit.k2r=r;akEdit.k2c=c;akEdit.key2=kc;}
      akSlot=null; draw(); return;
    }
    if(pickTarget){
      if(pickTarget==="k1")akK1=i; else if(pickTarget==="k2")akK2=i;
      pickTarget=null; showAkPicks(); return;}
    if(e.shiftKey||e.ctrlKey||mode==="multi"||mode==="light"||mode==="perf"){
      if(sel.has(i))sel.delete(i);else sel.add(i);
    }else{sel=new Set([i]);}
    if(mode==="perf"){const k=[...sel][0]; if(k!==undefined)setFocus(k);}
    draw();
  });
  const b=id=>root.querySelector("#"+id);
  if(b("s-all"))b("s-all").onclick=selectAll;
  if(b("s-inv"))b("s-inv").onclick=selectInvert;
  if(b("s-none"))b("s-none").onclick=selectNone;
}
function setFocus(i){
  const r=Math.floor(i/15),c=i%15;
  gaugeKey=i; gauge.reset();
  fetch("/api/focus",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({row:r,col:c})});
}

/* ---- travel gauge (unchanged behaviour) -------------------------------- */
const gauge={state:"IDLE",deepest:0,shallowest:0,
  reset(){this.state="IDLE";this.deepest=0;this.shallowest=0;},
  get pressed(){return this.state==="DOWN";},
  step(mm,actMm,rt){
    const useRT=rt&&rt.enabled;
    if(!useRT){this.state=mm>=actMm?"DOWN":"IDLE";return this.pressed;}
    switch(this.state){
      case "IDLE": if(mm>=actMm){this.state="DOWN";this.deepest=mm;} break;
      case "DOWN":
        if(mm>this.deepest)this.deepest=mm;
        if(mm<=this.deepest-rt.release_mm){this.state="UP";this.shallowest=mm;}
        else if(mm<actMm)this.state="IDLE";
        break;
      case "UP":
        if(mm<this.shallowest)this.shallowest=mm;
        if(mm<actMm)this.state="IDLE";
        else if(mm>=this.shallowest+rt.press_mm){this.state="DOWN";this.deepest=mm;}
        break;
    }
    return this.pressed;
  }};
function paintGauge(){
  const el=document.getElementById("gauge");
  if(!el||!latest||!latest.focus||gaugeKey===null)return;
  const f=latest.focus;
  if(idx(f.row,f.col)!==gaugeKey)return;
  const actMm=parseFloat(document.getElementById("rt-act")?.value)||1.5;
  const rt={enabled:document.getElementById("rt-on")?.checked||false,
    press_mm:parseFloat(document.getElementById("rt-press")?.value)||0.5,
    release_mm:parseFloat(document.getElementById("rt-rel")?.value)||0.5};
  const on=gauge.step(f.mm,actMm,rt);
  document.getElementById("g-fill").style.height=
    Math.max(0,Math.min(100,(f.mm/3.5)*100))+"%";
  el.classList.toggle("act",on);
  document.getElementById("g-mm").textContent=f.mm.toFixed(2)+" mm";
  document.getElementById("g-act").style.top=((actMm/3.5)*100)+"%";
  const rel=document.getElementById("g-rel");
  if(rt.enabled&&on){rel.style.display="";
    rel.style.top=((Math.max(0,gauge.deepest-rt.release_mm)/3.5)*100)+"%";}
  else rel.style.display="none";
  const st=document.getElementById("g-state");
  if(st){st.textContent=on?"PRESSED":"idle";st.classList.toggle("on",on);}
}

/* ---- profile plumbing --------------------------------------------------- */
async function openProfile(n){
  try{editDoc=await(await fetch(`/api/profile?name=${encodeURIComponent(n)}`)).json();
    if(editDoc.error)throw 0;}catch(e){toast("could not open profile",true);return;}
  editing=n; sel=new Set(); layer=0; gaugeKey=null;
  // Any half-finished advanced-key edit belongs to the profile being left.
  akEdit=null; akSlot=null; akK1=akK2=null; pickTarget=null; apExtra=0;
  try{await post("/api/apply-profile",{profile:n});}catch(e){}
  await loadConfig(); draw();
}
async function patchProfile(patch,apply=true){
  await post("/api/profile/edit",{name:editing,patch,apply});
  editDoc=await(await fetch(`/api/profile?name=${encodeURIComponent(editing)}`)).json();
  await loadConfig();
}
/* Which matrix keys the edit applies to: the selection, or everything if none. */
const targetKeys=()=>sel.size?[...sel]:populated();

/* ---- render ------------------------------------------------------------ */
/* Markup of the profile list as it was last written to the DOM.
   drawSidebar() runs from tick(), 16 times a second. It used to assign
   innerHTML unconditionally, which destroyed and rebuilt every button between
   a mousedown and its mouseup -- a `click` only fires when both land on the
   same element, so clicking a profile did nothing at all and the row just
   flickered as it was repainted. Rewriting only when the markup actually
   changes leaves the buttons alone the rest of the time. */
let plistHTML=null;
function drawSidebar(){
  const ob=(latest&&latest.onboard)||{};
  const eq=$("#nav-equipment"), mc=$("#nav-macros");
  if(eq)eq.className="navbtn"+(view==="equipment"?" on":"");
  if(mc)mc.className="navbtn"+(view==="macros"?" on":"");
  const html=profiles.map(p=>`<div class="prow${p.name===editing?" on":""}"
    data-p="${p.name}"><span class="pname">${p.name}</span>
    ${p.name===ob.profile
      ? '<span class="badge" title="Saved into the onboard memory on the keyboard">board</span>'
      : ''}
    ${p.name===defaultProfile
      ? '<span class="badge" title="Used when no app rule matches">default</span>'
      : ''}
    <button class="pmenu" data-pmenu="${p.name}"
      title="Rename, duplicate or delete">&#8942;</button></div>`).join("");
  if(html!==plistHTML){plistHTML=html;$("#plist").innerHTML=html;}
  const online=!!(latest&&latest.connected);
  $("#conn").className="conn"+(online?" on":"");
  $("#conn-text").textContent=online?"connected":"disconnected";
}
/* Delegated once, rather than reattached after each render: the buttons are
   replaced whenever the list changes, and a per-element handler would have to
   be rebound every time to survive it. */
addEventListener("click",e=>{
  const t=e.target;
  if(!t||!t.closest)return;
  // Any click outside the menu dismisses it.
  if(!t.closest("#ctxmenu"))closeCtxMenu();
  const dots=t.closest("#plist [data-pmenu]");
  if(dots){
    // Must not fall through to opening the profile underneath.
    e.stopPropagation();
    const r=dots.getBoundingClientRect();
    openProfileMenu(dots.dataset.pmenu,r.right-168,r.bottom+4);
    return;
  }
  const row=t.closest("#plist [data-p]");
  if(row)openProfile(row.dataset.p);
});
/* Sidebar sections. The previous tab is remembered so leaving Macros puts
   you back where you were rather than on the first tab. */
let tabBeforeMacros="Change Key Setting";
function setView(v){
  if(v===view)return;
  if(v==="macros"){tabBeforeMacros=tab; tab="Macro Setting";}
  else{tab=tabBeforeMacros;}
  view=v; sel=new Set(); akEdit=null; akSlot=null; draw();
}
function drawTabs(){
  const nav=$("#toptabs");
  if(view==="macros"){
    nav.innerHTML=`<button class="on">Macro Setting</button>`;
    return;
  }
  nav.innerHTML=TABS.map(t=>
    `<button class="${t===tab?"on":""}" data-t="${t}">${t}</button>`).join("");
  nav.querySelectorAll("[data-t]").forEach(b=>b.onclick=()=>{
    tab=b.dataset.t; sel=new Set(); akEdit=null; akSlot=null; draw();});
}
function draw(){
  drawSidebar(); drawTabs();
  const p=$("#page");
  // Macros are device-wide, so this view works with no profile selected.
  if(view==="macros"){
    if(!config){p.innerHTML=`<div class="panel sub">Loading…</div>`;return;}
    p.innerHTML=pageHTML(); wirePage(p); return;
  }
  if(!editing){p.innerHTML=`<div class="panel"><h2>No profile selected</h2>
    <div class="sub">Pick one on the left, or press + to make a new one.</div></div>`;
    return;}
  if(!config||!editDoc){p.innerHTML=`<div class="panel sub">Loading…</div>`;return;}
  p.innerHTML=pageHTML();
  wirePage(p);
}

function layerBar(names){
  return `<div class="layerbar">Edit Layer ${names.map((n,i)=>
    `<button data-layer="${i}" class="${i===layer?"":""}"
      ${i===layer?'style="background:var(--accent);border-color:transparent;color:#fff"':''}
      >${n}</button>`).join("")}</div>`;
}

function pageHTML(){
  switch(tab){

  case "Change Key Setting": {
    /* A full 100% keyboard, laid out in three blocks like the stock picker:
       main (15u), navigation (3u) and numpad (4u). Sizes are absolute so a key
       never stretches to fill its row -- which is what made Esc span the page. */
    const K=(lbl,kc,w)=>({lbl,kc,w:w||1});
    const GAP=w=>({gap:w});
    const main=[
      [K("Esc",0x29),GAP(1),K("F1",0x3a),K("F2",0x3b),K("F3",0x3c),K("F4",0x3d),
       GAP(0.5),K("F5",0x3e),K("F6",0x3f),K("F7",0x40),K("F8",0x41),
       GAP(0.5),K("F9",0x42),K("F10",0x43),K("F11",0x44),K("F12",0x45)],
      [K("`",0x35),K("1",0x1e),K("2",0x1f),K("3",0x20),K("4",0x21),K("5",0x22),
       K("6",0x23),K("7",0x24),K("8",0x25),K("9",0x26),K("0",0x27),K("-",0x2d),
       K("=",0x2e),K("Backspace",0x2a,2)],
      [K("Tab",0x2b,1.5),K("Q",0x14),K("W",0x1a),K("E",0x08),K("R",0x15),K("T",0x17),
       K("Y",0x1c),K("U",0x18),K("I",0x0c),K("O",0x12),K("P",0x13),K("[",0x2f),
       K("]",0x30),K("\\",0x31,1.5)],
      [K("Caps Lock",0x39,1.75),K("A",0x04),K("S",0x16),K("D",0x07),K("F",0x09),
       K("G",0x0a),K("H",0x0b),K("J",0x0d),K("K",0x0e),K("L",0x0f),K(";",0x33),
       K("'",0x34),K("Enter",0x28,2.25)],
      [K("Shift",0xe1,2.25),K("Z",0x1d),K("X",0x1b),K("C",0x06),K("V",0x19),
       K("B",0x05),K("N",0x11),K("M",0x10),K(",",0x36),K(".",0x37),K("/",0x38),
       K("Shift",0xe5,2.75)],
      [K("CTRL",0xe0,1.25),K("WIN",0xe3,1.25),K("ALT",0xe2,1.25),K("SPACE",0x2c,6.25),
       K("ALT",0xe6,1.25),K("WIN",0xe7,1.25),K("MENU",0x65,1.25),K("CTRL",0xe4,1.25)],
    ];
    const nav=[
      [K("PRTSC",0x46),K("LOCK",0x47),K("PAUSE",0x48)],
      [K("INS",0x49),K("HOME",0x4a),K("PGUP",0x4b)],
      [K("DEL",0x4c),K("END",0x4d),K("PGDN",0x4e)],
      [],
      [GAP(1),K("↑",0x52),GAP(1)],
      [K("←",0x50),K("↓",0x51),K("→",0x4f)],
    ];
    // Spacers subtract the flex gap exactly like keys do. Without that each
    // spacer added an extra 5px and the F-row crept past the Backspace edge.
    const cell=e=>e.gap!==undefined
      ? `<span style="width:calc(var(--pu) * ${e.gap} - 5px); flex:none"></span>`
      : `<div class="pk" data-kc="${e.kc}"
           style="width:calc(var(--pu) * ${e.w} - 5px); flex:none">${e.lbl}</div>`;
    const block=rows=>rows.map(r=>`<div class="pkrow">${r.map(cell).join("")}</div>`).join("");

    /* The numpad needs row spans for + and Enter, so it is a grid rather than
       rows of flex items. */
    const np=`<div class="numpad">
      <div class="pk" data-kc="0x53">LOCK</div>
      <div class="pk" data-kc="${0x54}">/</div>
      <div class="pk" data-kc="${0x55}">*</div>
      <div class="pk" data-kc="${0x56}">-</div>
      <div class="pk" data-kc="${0x5f}">7</div>
      <div class="pk" data-kc="${0x60}">8</div>
      <div class="pk" data-kc="${0x61}">9</div>
      <div class="pk tall" data-kc="${0x57}" style="grid-row: span 2">+</div>
      <div class="pk" data-kc="${0x5c}">4</div>
      <div class="pk" data-kc="${0x5d}">5</div>
      <div class="pk" data-kc="${0x5e}">6</div>
      <div class="pk" data-kc="${0x59}">1</div>
      <div class="pk" data-kc="${0x5a}">2</div>
      <div class="pk" data-kc="${0x5b}">3</div>
      <div class="pk tall" data-kc="${0x58}" style="grid-row: span 2">ENTER</div>
      <div class="pk" data-kc="${0x62}" style="grid-column: span 2">0</div>
      <div class="pk" data-kc="${0x63}">.</div>
    </div>`;

    return `<div id="kmbanner"></div>
      <div class="center">${boardHTML("keys")}</div>
      ${layerBar(["Normal Layer","FN1","FN2(Mac)","FN3"])}
      <div class="subtabs">${["Keyboard Key","System","Macro Key","Light Effect","Expand"]
        .map(s=>`<button data-pick="${s}" class="${s===pickTab?"on":""}"
          ${s==="Expand"?'disabled title="not implemented"':""}>${s}</button>`).join("")}</div>
      ${pickTab==="Keyboard Key"
        ? `<div class="picker"><div class="pkblocks">
             <div>${block(main)}</div><div>${block(nav)}</div>${np}</div></div>`
        : pickTab==="System"
        ? `<div class="picker"><div class="iconrow">${SYSTEM_KEYS.map(([g,t,kc])=>
             `<div class="pk icon" data-kc="${kc}" title="${t}">${g}</div>`).join("")}
           </div></div>`
        : pickTab==="Macro Key"
        ? `<div class="picker"><div class="iconrow">${
             Array.from({length:16},(_,i)=>
               `<div class="pk icon" data-kc="${0x7700+i}"
                  title="Macro M${i+1}${(config.macros?.[i]||[]).length
                    ?" — "+(config.macros[i].length)+" step(s)":" — empty"}"
                  >M${i+1}</div>`).join("")}
           </div>
           <div class="sub center" style="margin-top:12px">
             Binds one of the 16 onboard macros to the selected key. Record them
             on the Macro Setting tab.</div></div>`
        : `<div class="picker"><div class="iconrow">${RGB_KEYS.map(([g,t,kc])=>
             `<div class="pk icon" data-kc="${kc}" title="${t}">${g}</div>`).join("")}
           </div></div>`}
      <div class="sub center" style="margin-top:12px">
        Select a key on the board above, then click its new binding here.</div>`;
  }

  case "Lighting Setting": {
    const li=editDoc.light||{};
    const [cr,cg,cb]=liRGB(li);
    const hex=liHex(li);
    const bright=Math.round((Number(li.brightness||0)/BRIGHT_MAX)*100);
    const cur=EFFECT_BY_N[li.effect||0];
    const useCol=cur?cur[2]:1, useSpd=cur?cur[3]:0;
    // The Effect # box takes any number, so `cur` can be missing. Everything
    // below has to name the effect without assuming it is in the table.
    const curName=cur?cur[1]:`effect ${li.effect||0}`;
    // Per-key colours only mean anything in Customization. Under any other
    // effect the firmware owns every LED, so selecting keys is a dead end --
    // grey the board out rather than let it look like it should work.
    const perKey=(li.effect||0)===1;
    return `<div class="boardwrap${perKey?"":" dim"}"
                 title="${perKey?"":"Per-key colours apply only in 1. Customization"}"
            >${boardHTML("light")}${selbarHTML()}</div>
      <div class="panel" style="margin-top:20px">
        <div class="split" style="grid-template-columns:1fr 340px">
          <div class="left" style="border-right:1px solid var(--panel-brd)">
            <div class="row" style="align-items:flex-start">
              <b style="width:60px">Mode</b>
              <div class="modes" style="flex:1">${EFFECTS.map(([n,e,c,s])=>
                `<button data-eff="${n}" class="${(li.effect||0)===n?"on":""}"
                  title="effect ${n}${c?" · uses colour":" · generates its own colours"}${s?" · animated":" · static"}"
                  >${n}. ${e}</button>`).join("")}</div></div>
            <div class="row" style="margin-top:12px">
              <b style="width:60px">Effect #</b>
              <input type="number" id="li-effnum" min="0" max="255"
                value="${li.effect||0}" style="width:90px">
              <button id="li-effgo">Set</button>
              <span class="sub">Names and numbers come from the stock app's own
                table, so the list above is complete.</span></div>
            <div class="row" style="margin-top:6px">
              <b style="width:60px"></b>
              <span class="sub">${cur?`<b>${curName}</b> — ${useCol?"uses the colour below":"generates its own colours, so the colour picker is ignored"}, ${useSpd?"animated":"static (no speed)"}.`:"Unknown effect number."}</span></div>
            <div class="warn" style="margin-top:20px">
              Above 60% the board may draw enough power to become unstable.</div>
            <div class="row" style="margin-top:8px">
              <b style="width:90px">Brightness</b>
              <input type="range" id="li-bright" min="0" max="${BRIGHT_MAX}"
                value="${li.brightness||0}" style="flex:1">
              <span id="li-brightval" style="width:44px">${bright}%</span></div>
            <div class="row" style="margin-top:8px${useSpd?"":"; opacity:.4"}">
              <b style="width:90px">Speed</b>
              <input type="range" id="li-speed" min="1" max="255"
                value="${li.speed||DEFAULT_SPEED}" style="flex:1"
                ${useSpd?"":"disabled"}>
              <span id="li-speedval" style="width:44px">${li.speed||DEFAULT_SPEED}</span></div>
            <div class="row" style="margin-top:20px">
              <b style="width:90px">Save</b>
              <button class="primary" id="li-save">Save to keyboard</button>
              <span class="sub">stores the current lighting in flash</span></div>
            <div class="row" style="margin-top:20px">
              <b style="width:90px">Colours off?</b>
              <button id="cal-open">Colour calibration…</button>
              <span class="sub">correct the LEDs' colour balance</span></div>
          </div>
          <div class="right">
            <div class="row" style="align-items:flex-start"><b style="width:96px">Default<br>Color</b>
              <div class="swatches">${SWATCHES.map(c=>
                `<button data-sw="${c}" style="background:${c}"></button>`).join("")}</div></div>
            <div class="row" style="margin-top:22px; align-items:flex-start">
              <b style="width:96px">Custom<br>Color</b>
              <div style="flex:1">
                <input type="text" id="li-hex" value="${hex}" style="width:100%">
                <div class="row" style="margin-top:10px">
                  <label class="f">R <input type="number" id="li-r" min="0" max="255"
                    value="${cr}" style="width:64px"></label>
                  <label class="f">G <input type="number" id="li-g" min="0" max="255"
                    value="${cg}" style="width:64px"></label>
                  <label class="f">B <input type="number" id="li-b" min="0" max="255"
                    value="${cb}" style="width:64px"></label></div>
                <input type="color" id="li-color" value="${hex}"
                  style="width:100%; height:120px; margin-top:12px"
                  ${useCol?"":"disabled"}>
                <button class="primary" id="li-apply" style="width:100%; margin-top:12px"
                  ${useCol?"":"disabled"}>
                  ${!useCol ? "Colour not used by this effect"
                    : perKey ? `Apply to ${sel.size?sel.size+" selected key(s)":"all keys"}`
                    : `Apply colour to ${curName}`}</button>
                <div class="sub" style="margin-top:10px">
                  ${!useCol
                    ? `<b>${curName}</b> makes its own colours.
                       Pick <b>1. Customization</b> to colour keys yourself.`
                    : perKey
                    ? "Per-key mode is on, so these colours are what you see."
                    : `Sets the colour <b>${curName}</b> runs in.`}</div>
              </div></div>
          </div></div></div>`;
  }

  case "Performance": {
    const k=firstSel();
    const key=k!==null?`${Math.floor(k/15)},${k%15}`:null;
    const a=key?((editDoc.actuation?.overrides_mm||{})[key]??editDoc.actuation?.default_mm??1.5)
                :(editDoc.actuation?.default_mm??1.5);
    const rt=key?((editDoc.rapid_trigger?.overrides||{})[key]??editDoc.rapid_trigger?.default??{})
                :(editDoc.rapid_trigger?.default??{});
    const perf=editDoc.performance||{};
    const stepper=(id,v)=>`<div class="stepper">
      <button data-step="${id}" data-d="-1">&minus;</button>
      <input type="number" id="${id}" step="0.05" min="0.05" max="3.40" value="${v}">
      <span class="unit">mm</span><button data-step="${id}" data-d="1">+</button></div>`;

    let body="";
    if(subtab==="Travel"){
      body=`<div class="split">
        <div class="left">
          <h3>Trigger Travel</h3>
          <div class="blurb">The key triggers when pressed to a fixed depth and
            releases after rebounding past it. A shorter trigger travel responds
            faster, but makes accidental taps more likely.</div>
          <div style="display:flex;gap:18px;align-items:flex-start">
            <div class="gauge" id="gauge">
              <div class="fill" id="g-fill" style="height:0%"></div>
              <div class="mark" id="g-act" style="top:0"><span>trigger</span></div>
              <div class="mark rel" id="g-rel" style="top:0"><span>reset</span></div>
              <div class="readout" id="g-mm">—</div></div>
            <div><span class="state-pill" id="g-state">idle</span>
              <div class="sub" style="margin-top:10px">press the key<br>to watch it move</div></div>
          </div>
          <div style="margin-top:16px">${stepper("rt-act",a)}</div>
          <input type="range" id="rt-act-range" min="0.05" max="3.40" step="0.05"
            value="${a}" style="width:100%;margin-top:10px">
        </div>
        <div class="right">
          <div class="row" style="margin-bottom:6px">
            <h3 style="margin:0">Fast trigger mode (RT)</h3><span class="spacer"></span>
            <label class="switch"><input type="checkbox" id="rt-on"
              ${rt.enabled?"checked":""}><span></span></label></div>
          <div id="rt-body" class="${rt.enabled?"":"dim"}" style="margin-top:22px">
            <div class="setting"><div class="name">Rapid trigger travel</div>
              <div class="desc">When the key is pressed beyond the set trigger stroke
                and has released into the fast-reset state, pressing it again by this
                much triggers it once more.</div>
              <div class="sliderrow">
                <input type="range" id="rt-press-range" min="0.05" max="3.40" step="0.05"
                  value="${rt.press_mm??0.5}">${stepper("rt-press",rt.press_mm??0.5)}</div></div>
            <div class="setting"><div class="name">Quickly reset your travel</div>
              <div class="desc">When the key is pressed beyond the set trigger stroke,
                releasing it by this much stops the trigger immediately.</div>
              <div class="sliderrow">
                <input type="range" id="rt-rel-range" min="0.05" max="3.40" step="0.05"
                  value="${rt.release_mm??0.5}">${stepper("rt-rel",rt.release_mm??0.5)}</div></div>
          </div>
          <div class="row" style="margin-top:22px">
            <b>Advanced Settings</b><span class="spacer"></span>
            <button id="deadzone">Access Settings</button></div>
          <div class="row" style="margin-top:22px">
            <button class="primary" id="perf-apply">Apply to
              ${sel.size?sel.size+" selected key(s)":"all keys"}</button></div>
        </div></div>`;
    } else if(subtab==="Calibration"){
      body=`<div class="panel" style="background:transparent;border:0;padding:0">
        <h3>Calibration</h3>
        <div class="blurb" style="max-width:640px">Calibration teaches the keyboard
          the travel range of every switch. It is the one operation that can
          genuinely degrade a Hall-effect board if interrupted, so it is behind a
          typed confirmation. Only run it if keys read the wrong depth.</div>
        <div class="row" style="margin-top:16px">
          <button class="danger" id="cal-start">Start calibration</button>
          <button id="cal-finish">Finish calibration</button></div>
        <div class="sub" style="margin-top:12px">Between the two, press every key
          fully down and let it return, so the firmware sees each switch's range.</div>
        <div class="row" style="margin-top:22px">
          <span class="pill">calibrated keys: ${latest?latest.calibrated:"—"} / 75</span></div>
      </div>`;
    } else {
      const item=(id,name,desc,on)=>`<div class="toggle-item">
        <div><div class="t-name">${name}</div><div class="t-desc">${desc}</div></div>
        <label class="switch"><input type="checkbox" id="pf-${id}"
          ${on?"checked":""}><span></span></label></div>`;
      body=`<div class="toggles">
        ${item("wasd_switch","Swap WASD",
          "Switching WASD on will toggle the WASD press and arrow keys.",perf.wasd_switch)}
        ${item("mac_switch","MAC",
          "When you switch Mac mode, the default layer is switched to MacOS Base.",perf.mac_switch)}
        ${item("win_lock","Win lock","Win key will be locked.",perf.win_lock)}
        ${item("nkro_switch","Switch to 6-key rollover",
          "When enabled, it will switch from all-key rollover to 6-key rollover for special scenarios.",
          perf.nkro_switch)}
        ${item("bottom_optimize","False touch prevention mode",
          "Prevents accidental triggering from desk jitter or up to 0.1 mm of switch tolerance by optimising the bottom-key algorithm.",
          perf.bottom_optimize)}
        ${item("rgb_area","RGB area","Vendor RGB zone flag.",perf.rgb_area)}
        </div>
        <div class="row" style="margin-top:22px">
          <label class="f">Game mode <input type="number" id="pf-game_mode" min="0" max="255"
            value="${perf.game_mode||0}" style="width:80px"></label>
          <button class="primary" id="pf-go">Apply to profile</button></div>`;
    }

    return `${sel.size?"":`<div class="center"><div class="notice">
        <b>!</b> Select the keys below that you want to modify — with none selected,
        changes apply to every key.</div></div>`}
      <div class="boardwrap">${boardHTML("perf")}${selbarHTML()}</div>
      <div class="subtabs">${["Travel","Calibration","Performance"].map(s=>
        `<button data-sub="${s}" class="${s===subtab?"on":""}">${s}</button>`).join("")}</div>
      <div class="panel">${body}</div>`;
  }

  case "Advanced Key": {
    const aks=(editDoc.advanced_keys||[]).filter(a=>a.mode&&a.mode!=="NONE");
    const tds=(editDoc.tap_dance||[]).filter(t=>t.tap||t.hold);
    const dkss=(editDoc.dks||[]).filter(d2=>(d2.actions||[]).some(a=>a.keycode||a.status));
    const total=aks.length+tds.length+dkss.length;
    const SUBS=["Dynamic Key Stroke (DKS)","Hold/Click (MT)","Toggle Switch (TGL)",
                "Rappy Snappy (RS)","Sappy Tnappy (SOCD)"];

    const kn=(r,c)=>{const cell=config.layers?.[0]?.[r]?.[c];
      return cell?(LEGEND[cell.kc]||kcName(cell.kc)):"?";};
    let list="";
    if(akSub==="Rappy Snappy (RS)"||akSub==="Sappy Tnappy (SOCD)"){
      const want=akSub==="Rappy Snappy (RS)"?["RS"]
        :["SOCD","SOCD_KEY1","SOCD_KEY2","SOCD_BALANCE"];
      list=aks.filter(a=>want.includes(a.mode)).map(a=>`<div class="mstep">
        <span class="grip">#${a.index}</span>
        <span class="kc">${kn(a.key1_row,a.key1_col)}</span>
        <span style="padding:0 8px">+</span>
        <span class="kc">${kn(a.key2_row,a.key2_col)}</span>
        <span class="dir" style="font-size:12px">${AK_LABEL[a.mode]||a.mode}</span>
        <span class="ms">${(a.rs_apc_lv/100).toFixed(2)}mm${a.rt_sw?" · quick":""}</span>
        <span class="del" data-akopen="${a.index}">&#9998;</span>
        <span class="del" data-akdel="${a.index}">&times;</span></div>`).join("");
    } else if(akSub==="Hold/Click (MT)"){
      list=tds.map(t=>`<div class="mstep">
        <span class="grip">#${t.index}</span>
        <span class="kc">tap ${kcName(t.tap)}</span>
        <span class="kc" style="margin-left:8px">hold ${kcName(t.hold)}</span>
        <span class="ms">${t.timer_ms}ms</span>
        <span class="del" data-mtopen="${t.index}">&#9998;</span>
        <span class="del" data-mtdel="${t.index}">&times;</span></div>`).join("");
    } else if(akSub==="Dynamic Key Stroke (DKS)"){
      list=dkss.map(d2=>`<div class="mstep">
        <span class="grip">#${d2.index}</span>
        ${(d2.actions||[]).map(a=>`<span class="kc" style="margin-right:6px">
          ${a.keycode?kcName(a.keycode):"—"}</span>`).join("")}
        <span class="del" data-dksopen="${d2.index}">&#9998;</span>
        <span class="del" data-dksdel="${d2.index}">&times;</span></div>`).join("");
    }

    const unsupported=akSub==="Toggle Switch (TGL)";
    return `<div class="sub center">Each profile can hold up to 20 advanced keys.
        Currently bound: ${total}/20</div>
      <div class="center" style="margin-top:12px">${boardHTML("adv")}</div>
      ${layerBar(["Win Base","Win Fn","Mac Base","Mac Fn"])}
      <div class="subtabs">${SUBS.map(s=>{
        const off=s==="Toggle Switch (TGL)";
        return `<button data-aksub="${s}" class="${s===akSub?"on":""}"
          ${off?'disabled title="This controller has no toggle command"':""}
          >${s}</button>`;}).join("")}</div>
      <div class="panel" style="padding:0">
        ${akEdit
          ? advEditorHTML()
          : unsupported
          ? `<div class="sub" style="padding:18px">
             <b>Not available on this keyboard.</b><br>
             This board's controller has no toggle command.</div>`
          : `<div style="padding:18px">
             ${list||`<div class="sub">Nothing bound yet in this profile.</div>`}
             <div style="margin-top:14px">
               <button class="addbtn" id="ak-new">+ Add button</button></div></div>`}
      </div>`;
  }

  case "Macro Setting": {
    const ms=config.macros||[];
    return `<div class="mgrid">
      <div class="panel"><h2>Macro Functions</h2>
        <div class="plist">${ms.map((m,i)=>`<button data-m="${i}"
          class="${i===macroSel?"on":""}">M${i+1}
          ${Array.isArray(m)&&m.length?`<span class="badge">${m.length}</span>`:""}
          </button>`).join("")}</div></div>
      <div class="panel">
        <div class="row" style="margin-bottom:14px">
          <button id="mrec" class="${recording?"primary":""}">
            ${recording?"■ Stop":"▶ Start Record"}</button>
          <button id="mclear">Clear All</button>
          <button class="primary" id="msave">Save</button></div>
        <div id="msteps"></div>
        <div class="row" style="margin-top:12px">
          <select id="me-kind"><option value="tap">tap</option>
            <option value="down">down</option><option value="up">up</option>
            <option value="delay">delay</option><option value="text">text</option></select>
          <input type="text" id="me-key" placeholder="key" style="width:110px">
          <input type="number" id="me-ms" placeholder="ms" value="50" style="width:90px">
          <button id="me-add">Add step</button></div>
      </div>
      <div class="panel"><h2>Macro Setting</h2>
        <div class="sub">Macros live on the keyboard and are shared by every
          profile.</div></div></div>`;
  }

  case "App Trigger": {
    const isDefault=defaultProfile===editing;
    const apTriggers=docTriggers(editDoc);
    const other=defaultProfile&&!isDefault?defaultProfile:"";
    const off=isDefault?"disabled":"";
    return `<div class="panel" style="max-width:820px;margin:0 auto">
      <h2>App trigger</h2>
      <div class="sub">Choose the program that switches the keyboard to
        <b>${editing}</b>. Leave blank for a profile you only pick by hand.</div>

      <div class="defbox${isDefault?" on":""}">
        <label class="f" style="align-items:flex-start">
          <input type="checkbox" id="ap-default" ${isDefault?"checked":""}>
          <span><b>Make this the default profile</b>
            <div class="sub" style="margin-top:4px">
              Used when no other profile's app is in the foreground.${other
                ? ` <b>${other}</b> holds it now.` : ""}</div></span></label></div>

      <div id="ap-fields" class="${isDefault?"dim":""}" style="margin-top:18px">
        <div class="row">
          <select id="ap-proc" style="min-width:320px" ${off}>
            <option value="">— running programs —</option></select>
          <button id="ap-refresh" ${off}>Refresh</button>
          <button id="ap-browse" ${off}>Browse for .exe…</button></div>

        <div id="ap-list" style="margin-top:14px">${
          apTriggers.concat(Array.from(
            {length:apTriggers.length?apExtra:Math.max(1,apExtra)},
            ()=>({exe:"",title:""}))).map((t,i)=>`
          <div class="row aprow" data-i="${i}">
            <label class="f">executable <input type="text" class="ap-exe"
              value="${t.exe||""}" placeholder="e.g. cs2.exe"
              style="width:240px" ${off}></label>
            <label class="f">window title contains <input type="text" class="ap-title"
              value="${t.title||""}" placeholder="optional"
              style="width:220px" ${off}></label>
            <button class="iconbtn" data-aprm="${i}" title="Remove this app"
              ${off}>&times;</button>
          </div>`).join("")}</div>

        <div class="row" style="margin-top:12px">
          <button id="ap-add" ${off}>+ Add another app</button></div>
        <div class="row" style="margin-top:16px">
          <button class="primary" id="ap-save" ${off}>Save triggers</button>
          <button id="ap-clear" ${off}>Clear all</button></div>
      </div>

      <div class="sub" style="margin-top:16px">${isDefault
        ? `<b>${editing}</b> is the fallback, so it is not bound to any app.`
        : "Any one of these apps activates the profile. Where both fields are filled, both must match."}
      </div></div>`;
  }

  case "Other Settings": {
    const dev=(latest&&latest.device)||{};
    return `<div class="center">${boardHTML("keys")}</div>
      <div class="panel" style="max-width:760px;margin:22px auto 0">
        <div class="row" style="padding:14px 0;border-bottom:1px solid var(--panel-brd)">
          <div><b>Keyboard protocol version ${config.protocol_version}</b>
            <div class="sub">Flashing is not implemented here. The official
              configurator does it.</div></div>
          <button class="spacer" id="fw-open"
            title="Opens the official configurator in a new tab">Update firmware &#8599;</button></div>
        <div class="row" style="padding:14px 0;border-bottom:1px solid var(--panel-brd)">
          <div>The key bindings will be restored to the factory state.
            <div class="sub">Resets the dynamic keymap only.</div></div>
          <button class="danger spacer" id="reset-keys">Recover now</button></div>
        <div class="row" style="padding:14px 0;border-bottom:1px solid var(--panel-brd)">
          <div>Export <b>${editing}</b>
            <div class="sub">Saves this one profile to a file you can share.</div></div>
          <button class="spacer" id="prof-export">Export profile…</button></div>
        <div class="row" style="padding:14px 0">
          <div>Restore factory settings
            <div class="sub">Wipes the keyboard's onboard settings. A snapshot is
              written to backups/ first.</div></div>
          <button class="danger spacer" id="factory">Recover now</button></div>
      </div>`;
  }
  }
  return "";
}

let macroSel=0, recording=false, macroSteps=[], recLast=0;

/* ---- advanced-key editors ----------------------------------------------
   akEdit holds the entry being edited: {kind, index, ...fields}. Each editor
   mirrors the stock app's panel -- title bar with delete / Cancel / Save, the
   type description on the left, and the type's own controls on the right. */
let akEdit=null, akSlot=null;

function keyBox(kc,id,active){
  return `<div class="keybox${active?" picking":""}" data-kbox="${id}">
    ${kc?(KC[kc]!==undefined?KC[kc]:kcName(kc)):""}</div>`;
}

function advEditorHTML(){
  const e=akEdit;
  const head=(title)=>`<div class="edhead">
    <b>${title}</b>
    <span class="spacer"></span>
    <span class="del" id="ed-del" title="Delete this binding">&#128465;</span>
    <button id="ed-cancel">Cancel</button>
    <button class="primary" id="ed-save">Save</button></div>`;

  if(e.kind==="RS"){
    return `${head("Rappy Snappy (RS)")}
      <div class="edbody" style="grid-template-columns: 1fr 1fr">
        <div>
          <div class="edtype">Type: Rappy Snappy (RS)</div>
          <div class="blurb">Rappy Snappy watches two keys and activates whichever
            is pressed deeper. Press A, then press D further than A, and D takes
            over immediately; release D and A comes back.</div>
          <div class="keypair">
            <div><div class="klabel">Key1</div>${keyBox(e.key1,"key1",akSlot==="key1")}</div>
            <div><div class="klabel">Key2</div>${keyBox(e.key2,"key2",akSlot==="key2")}</div>
          </div>
        </div>
        <div class="edright">
          <div class="row"><b>Quick trigger</b><span class="spacer"></span>
            <label class="switch"><input type="checkbox" id="ed-quick"
              ${e.rt_sw?"checked":""}><span></span></label></div>
          <div class="setting" style="margin-top:22px">
            <div class="name">Trigger Travel</div>
            <div class="sliderrow" style="margin-top:10px">
              <input type="range" id="ed-apc" min="1" max="340" step="1"
                value="${e.rs_apc_lv||30}" style="flex:1">
              <span id="ed-apcv" style="width:70px; text-align:right">
                ${((e.rs_apc_lv||30)/100).toFixed(2)}mm</span></div>
          </div>
        </div>
      </div>`;
  }

  if(e.kind==="SOCD"){
    const PATTERNS=[
      ["SOCD","Last Input Priority",
       "The most recently pressed key wins. The classic counter-strafing setup."],
      ["SOCD_KEY1","Absolute Key1 Priority","Key 1 always wins while both are held."],
      ["SOCD_KEY2","Absolute Key2 Priority","Key 2 always wins while both are held."],
      ["SOCD_BALANCE","Neutral",
       "Holding both cancels out to nothing, like a physical switch."]];
    return `${head("Sappy Tnappy (SOCD)")}
      <div class="edbody" style="grid-template-columns: 1fr 1.1fr 1fr">
        <div>
          <div class="edtype">Type: Sappy Tnappy (SOCD)</div>
          <div class="blurb">SOCD (Simultaneous Opposing Cardinal Directions) decides
            what to do when two opposing direction keys are held at once — for
            example left and right — so the pair never fights itself.</div>
          <div class="keypair">
            <div><div class="klabel">Key1</div>${keyBox(e.key1,"key1",akSlot==="key1")}</div>
            <div><div class="klabel">Key2</div>${keyBox(e.key2,"key2",akSlot==="key2")}</div>
          </div>
        </div>
        <div>
          <div class="edtype" style="margin-bottom:12px">Behavioral patterns</div>
          ${PATTERNS.map(([m,label,help])=>`<label class="radiorow${e.mode===m?" on":""}"
            data-mode="${m}">
            <span class="radio${e.mode===m?" on":""}"></span>
            <span>${label}</span><span class="spacer"></span>
            <span class="qmark" title="${help}">?</span></label>`).join("")}
        </div>
        <div class="edright">
          <div class="row"><b>Quick trigger</b><span class="spacer"></span>
            <label class="switch"><input type="checkbox" id="ed-quick"
              ${e.rt_sw?"checked":""}><span></span></label></div>
          <div class="setting" style="margin-top:22px">
            <div class="name">Trigger Travel</div>
            <div class="sliderrow" style="margin-top:10px">
              <input type="range" id="ed-apc" min="1" max="340" step="1"
                value="${e.rs_apc_lv||30}" style="flex:1">
              <span id="ed-apcv" style="width:70px; text-align:right">
                ${((e.rs_apc_lv||30)/100).toFixed(2)}mm</span></div>
          </div>
        </div>
      </div>`;
  }

  if(e.kind==="MT"){
    return `${head("Hold/Click (MT) Key Magnetic Axis Advanced Keys")}
      <div style="padding:0 24px">
        <div class="blurb center" style="max-width:900px; margin:0 auto 18px">
          A single click and a long press each send a different keystroke. The long
          press can also switch layer, which greatly expands what one key can do.</div>
        <div class="keypair center" style="justify-content:center; gap:40px">
          <div><div class="klabel">Single<br>Click</div>
            ${keyBox(e.tap,"tap",akSlot==="tap")}</div>
          <div><div class="klabel">long<br>press</div>
            ${keyBox(e.hold,"hold",akSlot==="hold")}</div>
        </div>
        <div class="row" style="justify-content:center; margin-top:22px">
          <b>Press and hold time</b>
          <input type="range" id="ed-timer" min="10" max="1000" step="10"
            value="${e.timer_ms||200}" style="width:280px">
          <span id="ed-timerv" style="width:70px">${e.timer_ms||200}ms</span></div>
        <div class="blurb center" style="max-width:900px; margin:14px auto 0">
          A click key is slower than a normal keystroke because the firmware has to
          wait to see whether it is a tap or a hold. Turning quick trigger on for
          that key and lowering its reset distance helps, but it will never be as
          fast as a plain key.</div>
      </div>
      ${pickerHTML()}`;
  }

  if(e.kind==="DKS"){
    const ZONES=[["&#8615;","1.00mm"],["&#8615;","3.00mm"],["&#8613;","3.00mm"],["&#8613;","1.00mm"]];
    return `${head("Dynamic Key Stroke (DKS)")}
      <div style="padding:0 24px">
        <div class="blurb center" style="max-width:900px; margin:0 auto 18px">
          Binds one key to up to four keystrokes depending on how far it is pressed
          and released.</div>
        <div class="dksgrid">
          <div></div>
          ${ZONES.map(([ic,mm])=>`<div class="dkszone"><div class="ic">${ic}</div>
            <div class="mm">${mm}</div></div>`).join("")}
          ${[0,1,2,3].map(row=>`
            <div class="dksslot">${keyBox(e.actions[row]?.keycode,"dks"+row,
              akSlot==="dks"+row)}</div>
            ${[0,1,2,3].map(col=>{
              const on=((e.actions[row]?.status||0)>>col)&1;
              return `<div class="dkscell${on?" on":""}" data-dks="${row},${col}">
                ${on?"&#9679;":"+"}</div>`;}).join("")}
          `).join("")}
        </div>
        <div class="sub center" style="margin-top:14px">
          Each row is one keystroke; the four columns are the travel events
          that fire it.</div>
      </div>
      ${pickerHTML()}`;
  }
  return "";
}

/* The 100% picker, reused by the MT and DKS editors for choosing a keycode. */
function pickerHTML(){
  const K=(lbl,kc,w)=>({lbl,kc,w:w||1});
  const GAP=w=>({gap:w});
  const main=[
    [K("Esc",0x29),GAP(1),K("F1",0x3a),K("F2",0x3b),K("F3",0x3c),K("F4",0x3d),
     GAP(0.5),K("F5",0x3e),K("F6",0x3f),K("F7",0x40),K("F8",0x41),
     GAP(0.5),K("F9",0x42),K("F10",0x43),K("F11",0x44),K("F12",0x45)],
    [K("`",0x35),K("1",0x1e),K("2",0x1f),K("3",0x20),K("4",0x21),K("5",0x22),
     K("6",0x23),K("7",0x24),K("8",0x25),K("9",0x26),K("0",0x27),K("-",0x2d),
     K("=",0x2e),K("Backspace",0x2a,2)],
    [K("Tab",0x2b,1.5),K("Q",0x14),K("W",0x1a),K("E",0x08),K("R",0x15),K("T",0x17),
     K("Y",0x1c),K("U",0x18),K("I",0x0c),K("O",0x12),K("P",0x13),K("[",0x2f),
     K("]",0x30),K("\\",0x31,1.5)],
    [K("Caps Lock",0x39,1.75),K("A",0x04),K("S",0x16),K("D",0x07),K("F",0x09),
     K("G",0x0a),K("H",0x0b),K("J",0x0d),K("K",0x0e),K("L",0x0f),K(";",0x33),
     K("'",0x34),K("Enter",0x28,2.25)],
    [K("Shift",0xe1,2.25),K("Z",0x1d),K("X",0x1b),K("C",0x06),K("V",0x19),
     K("B",0x05),K("N",0x11),K("M",0x10),K(",",0x36),K(".",0x37),K("/",0x38),
     K("Shift",0xe5,2.75)],
    [K("CTRL",0xe0,1.25),K("WIN",0xe3,1.25),K("ALT",0xe2,1.25),K("SPACE",0x2c,6.25),
     K("ALT",0xe6,1.25),K("WIN",0xe7,1.25),K("MENU",0x65,1.25),K("CTRL",0xe4,1.25)],
  ];
  const nav=[
    [K("PRTSC",0x46),K("LOCK",0x47),K("PAUSE",0x48)],
    [K("INS",0x49),K("HOME",0x4a),K("PGUP",0x4b)],
    [K("DEL",0x4c),K("END",0x4d),K("PGDN",0x4e)],
    [],
    [GAP(1),K("↑",0x52),GAP(1)],
    [K("←",0x50),K("↓",0x51),K("→",0x4f)],
  ];
  const cell=e=>e.gap!==undefined
    ? `<span style="width:calc(var(--pu) * ${e.gap} - 5px); flex:none"></span>`
    : `<div class="pk" data-kc="${e.kc}"
         style="width:calc(var(--pu) * ${e.w} - 5px); flex:none">${e.lbl}</div>`;
  const block=rows=>rows.map(r=>`<div class="pkrow">${r.map(cell).join("")}</div>`).join("");
  const np=`<div class="numpad">
    <div class="pk" data-kc="${0x53}">LOCK</div><div class="pk" data-kc="${0x54}">/</div>
    <div class="pk" data-kc="${0x55}">*</div><div class="pk" data-kc="${0x56}">-</div>
    <div class="pk" data-kc="${0x5f}">7</div><div class="pk" data-kc="${0x60}">8</div>
    <div class="pk" data-kc="${0x61}">9</div>
    <div class="pk" data-kc="${0x57}" style="grid-row: span 2">+</div>
    <div class="pk" data-kc="${0x5c}">4</div><div class="pk" data-kc="${0x5d}">5</div>
    <div class="pk" data-kc="${0x5e}">6</div>
    <div class="pk" data-kc="${0x59}">1</div><div class="pk" data-kc="${0x5a}">2</div>
    <div class="pk" data-kc="${0x5b}">3</div>
    <div class="pk" data-kc="${0x58}" style="grid-row: span 2">ENTER</div>
    <div class="pk" data-kc="${0x62}" style="grid-column: span 2">0</div>
    <div class="pk" data-kc="${0x63}">.</div></div>`;
  return `<div class="picker" style="margin-top:20px"><div class="pkblocks">
    <div>${block(main)}</div><div>${block(nav)}</div>${np}</div></div>`;
}

function wireAdvEditor(root){
  const el=id=>root.querySelector("#"+id);
  const e=akEdit;

  root.querySelectorAll("[data-kbox]").forEach(b=>b.onclick=()=>{
    akSlot=b.dataset.kbox;
    if(e.kind==="RS"||e.kind==="SOCD")
      toast("now click a key on the board above");
    else toast("now click a key in the picker below");
    draw();
  });
  root.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{
    e.mode=b.dataset.mode; draw();});
  root.querySelectorAll("[data-dks]").forEach(b=>b.onclick=()=>{
    const [r,c]=b.dataset.dks.split(",").map(Number);
    e.actions[r]=e.actions[r]||{keycode:0,status:0};
    e.actions[r].status^=(1<<c);
    draw();});
  root.querySelectorAll("[data-kc]").forEach(b=>b.onclick=()=>{
    if(!akSlot){toast("pick a slot above first",true);return;}
    const kc=+b.dataset.kc;
    if(akSlot==="tap")e.tap=kc;
    else if(akSlot==="hold")e.hold=kc;
    else if(akSlot.startsWith("dks")){
      const r=+akSlot.slice(3);
      e.actions[r]=e.actions[r]||{keycode:0,status:0};
      e.actions[r].keycode=kc;
    }
    akSlot=null; draw();});

  const apc=el("ed-apc");
  if(apc)apc.oninput=()=>{el("ed-apcv").textContent=(apc.value/100).toFixed(2)+"mm";};
  const tm=el("ed-timer");
  if(tm)tm.oninput=()=>{el("ed-timerv").textContent=tm.value+"ms";};

  if(el("ed-cancel"))el("ed-cancel").onclick=()=>{akEdit=null;akSlot=null;draw();};
  if(el("ed-del"))el("ed-del").onclick=async()=>{
    if(!confirm("Delete this binding?"))return;
    if(e.kind==="MT")
      await patchProfile({tap_dance:{index:e.index,tap:0,hold:0,timer_ms:200}});
    else if(e.kind==="DKS")
      await patchProfile({dks:{index:e.index,
        actions:[0,1,2,3].map(()=>({keycode:0,status:0}))}});
    else
      await patchProfile({advanced_key:{index:e.index,mode:"NONE",id:0,rs_apc_lv:0,
        gapc_sw:0,rt_sw:0,key1_row:0,key1_col:0,key2_row:0,key2_col:0,layer:0}});
    akEdit=null;akSlot=null;toast("deleted");draw();};

  if(el("ed-save"))el("ed-save").onclick=async()=>{
    if(e.kind==="MT"){
      if(!e.tap&&!e.hold){toast("set at least one keystroke",true);return;}
      await patchProfile({tap_dance:{index:e.index,tap:e.tap||0,hold:e.hold||0,
        timer_ms:+(el("ed-timer")?.value||200)}});
    } else if(e.kind==="DKS"){
      await patchProfile({dks:{index:e.index,
        actions:[0,1,2,3].map(i=>e.actions[i]||{keycode:0,status:0})}});
    } else {
      if(e.key1===null||e.key2===null||e.key1===undefined||e.key2===undefined){
        toast("pick both keys first",true);return;}
      await patchProfile({advanced_key:{index:e.index,
        mode:e.kind==="RS"?"RS":e.mode,id:0,
        rs_apc_lv:+(el("ed-apc")?.value||30),gapc_sw:0,
        rt_sw:el("ed-quick")?.checked?1:0,
        key1_row:e.k1r,key1_col:e.k1c,key2_row:e.k2r,key2_col:e.k2c,layer:0}});
    }
    akEdit=null;akSlot=null;toast("saved");draw();};
}

function wirePage(root){
  const el=id=>root.querySelector("#"+id);
  const on=(id,fn)=>{const e=el(id);if(e)e.onclick=fn;};
  const val=id=>{const e=el(id);return e?e.value:null;};
  const chk=id=>{const e=el(id);return e?e.checked:false;};
  const hex2rgb=h=>[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];

  wireBoard(root, tab==="Performance"?"perf":tab==="Lighting Setting"?"light":"single");
  root.querySelectorAll("[data-layer]").forEach(b=>b.onclick=()=>{layer=+b.dataset.layer;draw();});
  root.querySelectorAll("[data-sub]").forEach(b=>b.onclick=()=>{subtab=b.dataset.sub;draw();});
  root.querySelectorAll("[data-aksub]").forEach(b=>b.onclick=()=>{
    akSub=b.dataset.aksub; akEdit=null; akSlot=null;
    akK1=akK2=null; pickTarget=null; draw();});
  root.querySelectorAll("[data-step]").forEach(b=>b.onclick=()=>{
    const inp=el(b.dataset.step);if(!inp)return;
    inp.value=Math.max(0.05,Math.min(3.40,(parseFloat(inp.value)||0)+0.05*(+b.dataset.d))).toFixed(2);
    inp.dispatchEvent(new Event("input"));});

  if(tab==="Change Key Setting"){
    drawKeymapNote();
    root.querySelectorAll("[data-pick]").forEach(b=>b.onclick=()=>{
      pickTab=b.dataset.pick; draw();});
    root.querySelectorAll("[data-kc]").forEach(b=>b.onclick=async()=>{
      const i=firstSel();
      if(i===null){toast("select a key on the board first",true);return;}
      // The profile name goes along so the edit is mirrored into its stored
      // keymap, not just written to the board.
      await post("/api/keycode",{layer,row:Math.floor(i/15),col:i%15,
                                 keycode:+b.dataset.kc,profile:editing});
      toast("key reassigned"); await loadConfig(); draw();});
  }

  if(tab==="Lighting Setting"){
    const syncHex=()=>{const h=val("li-hex");
      if(/^#[0-9a-f]{6}$/i.test(h)){const[r,g,b]=hex2rgb(h);
        el("li-r").value=r;el("li-g").value=g;el("li-b").value=b;el("li-color").value=h;}};
    if(el("li-hex"))el("li-hex").oninput=syncHex;
    if(el("li-color"))el("li-color").oninput=()=>{el("li-hex").value=el("li-color").value;syncHex();};
    ["li-r","li-g","li-b"].forEach(id=>{if(el(id))el(id).oninput=()=>{
      const h="#"+["li-r","li-g","li-b"].map(x=>
        Math.max(0,Math.min(255,+val(x)||0)).toString(16).padStart(2,"0")).join("");
      el("li-hex").value=h;el("li-color").value=h;};});
    if(el("li-bright")){
      el("li-bright").oninput=()=>{
        el("li-brightval").textContent=
          Math.round((+val("li-bright")/BRIGHT_MAX)*100)+"%";};
      // Commit on release. Without this the slider was only read by Apply, so
      // picking an effect wrote the profile's stale brightness back and the bar
      // snapped to 0.
      el("li-bright").onchange=async()=>{
        await patchProfile({light:{...(editDoc.light||{}),
          brightness:+val("li-bright")}});
        toast(`brightness ${Math.round((+val("li-bright")/BRIGHT_MAX)*100)}%`);};
    }
    const liSpeed=()=>{
      const e=el("li-speed"); const v=e?+e.value:0;
      return (v>=1&&v<=255)?v:DEFAULT_SPEED;};
    if(el("li-speed")){
      el("li-speed").oninput=()=>{
        el("li-speedval").textContent=val("li-speed");};
      el("li-speed").onchange=async()=>{
        await patchProfile({light:{...(editDoc.light||{}),
          speed:+val("li-speed")}});
        toast(`speed ${val("li-speed")}`);};
    }
    root.querySelectorAll("[data-sw]").forEach(b=>b.onclick=()=>{
      el("li-hex").value=b.dataset.sw;syncHex();});
    root.querySelectorAll("[data-eff]").forEach(b=>b.onclick=async()=>{
      // Carry whatever the sliders currently show, not the stored values, so
      // switching effect never silently resets brightness, speed or colour.
      const li=editDoc.light||{};
      // Never fall back to black here: picking a colour-driven effect would
      // then write black over the profile and kill the effect it just enabled.
      const [r,g,b2]=hex2rgb(val("li-hex")||liHex(li));
      await patchProfile({light:{...li,effect:+b.dataset.eff,
        brightness:+val("li-bright"),speed:liSpeed(),r,g,b:b2}});
      toast(`effect ${b.dataset.eff} applied`);draw();});
    on("li-effgo",async()=>{
      await patchProfile({light:{...(editDoc.light||{}),
        effect:+val("li-effnum"),speed:liSpeed()}});
      toast(`effect ${val("li-effnum")} applied`);draw();});
    on("li-apply",async()=>{
      const[r,g,b]=hex2rgb(val("li-hex"));
      const li=editDoc.light||{}, fx=li.effect||0;
      const info=EFFECT_BY_N[fx];
      if(!info||!info[2]){toast("this effect makes its own colours",true);return;}
      if(fx===1){
        // Customization is the only effect that reads the per-key table.
        await patchProfile({
          key_colors_keys:targetKeys().map(i=>({index:i,r,g,b})),
          light:{...li,effect:1,r,g,b,brightness:+val("li-bright")}});
        toast(`coloured ${sel.size||75} key(s)`);
      }else{
        // Every other colour-capable effect takes a single colour from the
        // LightInfo packet. Recolour it in place; hijacking the effect back to
        // Customization is not what the button says it does.
        await patchProfile({light:{...li,r,g,b,
          brightness:+val("li-bright"),speed:liSpeed()}});
        toast(`${info[1]} recoloured`);
      }
      draw();});
    on("li-save",async()=>{await post("/api/save-flash",{what:"lighting"});
      toast("lighting saved to the keyboard");});

    /* ---- live colour calibration, in a modal ----
       It paints the whole board to preview a colour, so it has to put the
       lighting back the way it found it when the dialog closes. */
    on("cal-open",async()=>{
      const before={...(editDoc.light||{})};
      $("#modal").innerHTML=`<div class="modal"><div class="box" style="max-width:640px">
        <h2>Colour calibration</h2>
        <div class="sub" style="margin-top:8px">
          The LEDs are not colour-balanced, so the colour you ask for and the
          one you see differ. Pick a test colour and drag until the board
          matches it — it updates live.</div>
        <div class="row" style="margin:16px 0 10px">
          <span class="lab">Test colour</span>
          ${["#ff0000","#ff00ff","#ffff00","#ffffff","#00ff00","#0000ff",
             "#ff8000","#8000ff"].map(c=>
            `<button class="swatchbtn" data-test="${c}"
               style="background:${c}"></button>`).join("")}
          <input type="color" id="cal-custom" value="#ff0000">
        </div>
        ${[["r","Red"],["g","Green"],["b","Blue"]].map(([ch,label])=>`
          <div class="row" style="margin-bottom:6px">
            <span class="lab">${label}</span>
            <input type="range" id="cal-${ch}" min="0.05" max="1" step="0.01"
              value="1" style="flex:1">
            <span id="cal-${ch}v" style="width:52px; text-align:right">1.00</span>
          </div>`).join("")}
        <div class="row" style="margin-top:20px">
          <button id="cal-reset">Reset to neutral</button>
          <span class="spacer"></span>
          <span class="sub" id="cal-note"></span>
          <button id="cal-close">Close</button>
          <button class="primary" id="cal-save">Save calibration</button></div>
      </div></div>`;
      const m=$("#modal"), q=id=>m.querySelector("#"+id);
      let calColour={r:255,g:0,b:0};
      const calGain=()=>({r:+q("cal-r").value,g:+q("cal-g").value,
                          b:+q("cal-b").value});
      const calPush=async()=>{
        ["r","g","b"].forEach(ch=>{
          q("cal-"+ch+"v").textContent=(+q("cal-"+ch).value).toFixed(2);});
        try{await post("/api/led-gain",{...calGain(),preview:calColour,
                                        brightness:BRIGHT_MAX});}catch(e){}
      };
      fetch("/api/led-gain").then(r=>r.json()).then(g=>{
        if(!q("cal-r"))return;
        ["r","g","b"].forEach(ch=>{
          q("cal-"+ch).value=g[ch];
          q("cal-"+ch+"v").textContent=(+g[ch]).toFixed(2);});
      });
      ["r","g","b"].forEach(ch=>{
        q("cal-"+ch).oninput=()=>{
          q("cal-"+ch+"v").textContent=(+q("cal-"+ch).value).toFixed(2);};
        q("cal-"+ch).onchange=calPush;});
      m.querySelectorAll("[data-test]").forEach(b=>b.onclick=()=>{
        calColour=hexToObj(b.dataset.test); q("cal-custom").value=b.dataset.test;
        calPush();});
      q("cal-custom").onchange=()=>{
        calColour=hexToObj(q("cal-custom").value); calPush();};
      q("cal-save").onclick=async()=>{
        await post("/api/led-gain",{...calGain(),persist:true});
        q("cal-note").textContent="saved";toast("calibration saved");};
      q("cal-reset").onclick=async()=>{
        ["r","g","b"].forEach(ch=>{q("cal-"+ch).value=1;});
        await calPush();toast("calibration reset to neutral");};
      q("cal-close").onclick=async()=>{
        m.innerHTML="";
        // Put back the effect/colour the preview painted over.
        await patchProfile({light:before});draw();};
    });
  }

  if(tab==="Performance"&&subtab==="Travel"){
    const link=(r,n)=>{const a=el(r),b=el(n);if(!a||!b)return;
      a.oninput=()=>{b.value=parseFloat(a.value).toFixed(2);};
      b.oninput=()=>{a.value=b.value;};};
    link("rt-act-range","rt-act");link("rt-press-range","rt-press");link("rt-rel-range","rt-rel");
    if(el("rt-on"))el("rt-on").onchange=()=>{
      el("rt-body").classList.toggle("dim",!el("rt-on").checked);gauge.reset();};
    on("perf-apply",async()=>{
      const keys=targetKeys().map(i=>`${Math.floor(i/15)},${i%15}`);
      await patchProfile({
        actuation_keys:keys.map(k=>({key:k,mm:parseFloat(val("rt-act"))})),
        rt_keys:keys.map(k=>({key:k,enabled:chk("rt-on"),
          press_mm:parseFloat(val("rt-press")),release_mm:parseFloat(val("rt-rel"))}))});
      gauge.reset();toast(`applied to ${keys.length} key(s)`);draw();});
    on("deadzone",()=>{
      const p=editDoc.performance||{};
      $("#modal").innerHTML=`<div class="modal"><div class="box">
        <h2>Dead zone setting</h2>
        <div class="grid2" style="margin-top:14px">
          <div><b>Press (trigger)</b>
            <div class="row" style="margin-top:8px"><span class="pill">High</span>
              <input type="range" id="dz-top" min="0" max="0.5" step="0.01"
                value="${p.dead_band_top||0}" style="flex:1">
              <span class="pill">Low</span>
              <span id="dz-topv" style="width:64px">${(p.dead_band_top||0).toFixed(2)}mm</span></div>
            <div class="warn" style="margin-top:8px">With a dead zone of 0 a slight
              finger shake can be judged as a key press.</div></div>
          <div><b>Lift (reset)</b>
            <div class="row" style="margin-top:8px"><span class="pill">High</span>
              <input type="range" id="dz-bot" min="0" max="0.5" step="0.01"
                value="${p.dead_band_bottom||0}" style="flex:1">
              <span class="pill">Low</span>
              <span id="dz-botv" style="width:64px">${(p.dead_band_bottom||0).toFixed(2)}mm</span></div>
            <div class="warn" style="margin-top:8px">With a dead zone of 0 a slight
              finger shake can be judged as a key release.</div></div></div>
        <div class="row" style="margin-top:20px"><span class="spacer"></span>
          <button id="dz-cancel">Cancel</button>
          <button class="primary" id="dz-ok">Complete</button></div>
      </div></div>`;
      const m=$("#modal");
      const sync=(a,b)=>{m.querySelector("#"+a).oninput=()=>{
        m.querySelector("#"+b).textContent=(+m.querySelector("#"+a).value).toFixed(2)+"mm";};};
      sync("dz-top","dz-topv");sync("dz-bot","dz-botv");
      m.querySelector("#dz-cancel").onclick=()=>{m.innerHTML="";};
      m.querySelector("#dz-ok").onclick=async()=>{
        await patchProfile({performance:{...(editDoc.performance||{}),
          dead_band_top:+m.querySelector("#dz-top").value,
          dead_band_bottom:+m.querySelector("#dz-bot").value}});
        m.innerHTML="";toast("dead zone applied");};
    });
  }

  if(tab==="Performance"&&subtab==="Calibration"){
    on("cal-start",async()=>{
      if(prompt("Calibration can degrade the board if interrupted.\\n"+
        "Type CALIBRATE to start:")!=="CALIBRATE"){toast("cancelled");return;}
      await post("/api/calibrate",{action:"start",confirm:"CALIBRATE"});
      toast("calibration started — press every key fully, then Finish");});
    on("cal-finish",async()=>{
      await post("/api/calibrate",{action:"finish",confirm:"CALIBRATE"});
      toast("calibration finished");});
  }

  if(tab==="Performance"&&subtab==="Performance"){
    on("pf-go",async()=>{
      const perf={...(editDoc.performance||{})};
      ["wasd_switch","mac_switch","win_lock","nkro_switch","bottom_optimize","rgb_area"]
        .forEach(f=>perf[f]=chk("pf-"+f)?1:0);
      perf.game_mode=+val("pf-game_mode");
      await patchProfile({performance:perf});toast("performance applied");});
  }

  if(tab==="Advanced Key"){
    if(akEdit){wireAdvEditor(root);return;}
    root.querySelectorAll("[data-akopen]").forEach(b=>b.onclick=()=>{
      const a=(editDoc.advanced_keys||[]).find(x=>x.index===+b.dataset.akopen);
      if(!a)return;
      akEdit={kind:a.mode==="RS"?"RS":"SOCD",index:a.index,mode:a.mode,
        rs_apc_lv:a.rs_apc_lv,rt_sw:a.rt_sw,
        k1r:a.key1_row,k1c:a.key1_col,k2r:a.key2_row,k2c:a.key2_col,
        key1:(config.layers?.[0]?.[a.key1_row]?.[a.key1_col]||{}).kc,
        key2:(config.layers?.[0]?.[a.key2_row]?.[a.key2_col]||{}).kc};
      draw();});
    root.querySelectorAll("[data-mtopen]").forEach(b=>b.onclick=()=>{
      const t=(editDoc.tap_dance||[]).find(x=>x.index===+b.dataset.mtopen);
      if(!t)return;
      akEdit={kind:"MT",index:t.index,tap:t.tap,hold:t.hold,timer_ms:t.timer_ms};
      draw();});
    root.querySelectorAll("[data-dksopen]").forEach(b=>b.onclick=()=>{
      const d2=(editDoc.dks||[]).find(x=>x.index===+b.dataset.dksopen);
      if(!d2)return;
      akEdit={kind:"DKS",index:d2.index,
        actions:JSON.parse(JSON.stringify(d2.actions||[]))};
      draw();});
    const nextFree=(list,key)=>{
      const used=new Set((list||[]).map(x=>x.index));
      for(let i=0;i<16;i++)if(!used.has(i))return i;
      return null;};
    if(root.querySelector("#ak-new"))root.querySelector("#ak-new").onclick=()=>{
      if(akSub==="Hold/Click (MT)"){
        const i=nextFree(editDoc.tap_dance);
        if(i===null){toast("no free slot",true);return;}
        akEdit={kind:"MT",index:i,tap:0,hold:0,timer_ms:200};
      } else if(akSub==="Dynamic Key Stroke (DKS)"){
        const i=nextFree(editDoc.dks);
        if(i===null){toast("no free slot",true);return;}
        akEdit={kind:"DKS",index:i,
          actions:[0,1,2,3].map(()=>({keycode:0,status:0}))};
      } else {
        const used=new Set((editDoc.advanced_keys||[])
          .filter(a=>a.mode&&a.mode!=="NONE").map(a=>a.index));
        let i=0; while(used.has(i)&&i<16)i++;
        if(i>=16){toast("no free slot",true);return;}
        akEdit={kind:akSub==="Rappy Snappy (RS)"?"RS":"SOCD",index:i,
          mode:akSub==="Rappy Snappy (RS)"?"RS":"SOCD",rs_apc_lv:30,rt_sw:1,
          k1r:null,k1c:null,k2r:null,k2c:null,key1:null,key2:null};
      }
      akSlot=null; draw();};
    showAkPicks();
    on("ak-pick1",()=>{pickTarget="k1";toast("click a key on the board");});
    on("ak-pick2",()=>{pickTarget="k2";toast("click a key on the board");});
    on("ak-add",async()=>{
      const k1=akK1,k2=akK2;
      if(k1===null||k2===null){toast("pick both keys first",true);return;}
      const used=(editDoc.advanced_keys||[]).filter(a=>a.mode&&a.mode!=="NONE");
      if(used.length>=20){toast("20 advanced keys is the limit",true);return;}
      const free=[...Array(16).keys()].find(i=>!used.some(a=>a.index===i));
      if(free===undefined){toast("no free slot",true);return;}
      const modeName=akSub==="Rappy Snappy (RS)"?"RS":
        ({2:"SOCD",3:"SOCD_KEY1",4:"SOCD_KEY2",5:"SOCD_BALANCE"})[+(val("ak-prio")||2)];
      await patchProfile({advanced_key:{index:free,mode:modeName,id:0,
        rs_apc_lv:+val("ak-apc"),gapc_sw:0,rt_sw:1,
        key1_row:Math.floor(k1/15),key1_col:k1%15,
        key2_row:Math.floor(k2/15),key2_col:k2%15,layer:0}});
      akK1=akK2=null; pickTarget=null;
      toast("advanced key added");draw();});
    root.querySelectorAll("[data-akdel]").forEach(b=>b.onclick=async()=>{
      await patchProfile({advanced_key:{index:+b.dataset.akdel,mode:"NONE",id:0,
        rs_apc_lv:0,gapc_sw:0,rt_sw:0,key1_row:0,key1_col:0,key2_row:0,key2_col:0,layer:0}});
      toast("cleared");draw();});
  }

  if(tab==="Macro Setting"){
    const drawSteps=()=>{
      el("msteps").innerHTML=macroSteps.length?macroSteps.map((s,i)=>`<div class="mstep">
        <span class="grip">&#8942;&#8942;</span>
        <span class="dir">${s.kind==="down"?"↓":s.kind==="up"?"↑":
          s.kind==="delay"?"⏱":"•"}</span>
        <span class="kc">${s.kind==="text"?s.text:s.kind==="delay"?"":kcName(s.keycode)}</span>
        <span class="ms">${s.kind==="delay"?s.delay_ms+"ms":""}</span>
        <span class="del" data-ds="${i}">&#128465;</span></div>`).join("")
        :`<div class="sub">No steps. Record, or add them by hand.</div>`;
      root.querySelectorAll("[data-ds]").forEach(a=>a.onclick=()=>{
        macroSteps.splice(+a.dataset.ds,1);drawSteps();});};
    root.querySelectorAll("[data-m]").forEach(b=>b.onclick=()=>{
      macroSel=+b.dataset.m;
      macroSteps=JSON.parse(JSON.stringify((config.macros||[])[macroSel]||[]));
      draw();});
    macroSteps=macroSteps.length?macroSteps
      :JSON.parse(JSON.stringify((config.macros||[])[macroSel]||[]));
    drawSteps();
    on("mrec",()=>{recording=!recording;recLast=performance.now();
      toast(recording?"recording — type now, click Stop when done":"recording stopped");
      draw();});
    on("mclear",()=>{macroSteps=[];drawSteps();});
    on("me-add",()=>{
      const kind=val("me-kind");
      if(kind==="delay")macroSteps.push({kind:"delay",delay_ms:+val("me-ms")||0});
      else if(kind==="text")macroSteps.push({kind:"text",text:val("me-key")||""});
      else{const kc=parseKey(val("me-key"));
        if(kc===null){toast("unrecognised key",true);return;}
        macroSteps.push({kind,keycode:kc});}
      drawSteps();});
    on("msave",async()=>{
      await post("/api/macro/set",{index:macroSel,steps:macroSteps});
      toast(`M${macroSel+1} written to the keyboard`);await loadConfig();draw();});
  }

  if(tab==="App Trigger"){
    const isDefault=defaultProfile===editing;
    if(el("ap-default"))el("ap-default").onchange=async()=>{
      const want=el("ap-default").checked;
      try{
        await post("/api/profile/default",{name:editing,default:want});
        await refreshProfiles();
        editDoc=await(await fetch(
          `/api/profile?name=${encodeURIComponent(editing)}`)).json();
        toast(want?`${editing} is now the default profile`
                  :`${editing} is no longer the default`);
      }catch(e){}
      draw();};
    // The app fields are disabled while this profile is the fallback, so they
    // are left unwired -- a handler there could only produce a save that
    // contradicts the checkbox. Scoped with a block rather than an early
    // `return`, which would also skip any wiring added after this one.
    if(!isDefault){
      const fill=async()=>{const s2=el("ap-proc");
        s2.innerHTML=`<option value="">— running programs —</option>`;
        try{const j=await(await fetch("/api/processes")).json();
          for(const p of j.processes||[]){const o=document.createElement("option");
            o.value=p.exe;o.textContent=`${p.exe}  —  ${(p.title||"").slice(0,40)}`;
            s2.appendChild(o);}}catch(e){}};
      fill();
      const rows=()=>[...root.querySelectorAll(".aprow")];
      const read=()=>rows().map(r=>({
        exe:r.querySelector(".ap-exe").value.trim(),
        title:r.querySelector(".ap-title").value.trim()}))
        .filter(t=>t.exe||t.title);
      // The picker and Browse fill the row the user last clicked into, so with
      // several apps listed they do not always overwrite the first one.
      let lastRow=0;
      root.querySelectorAll(".ap-exe,.ap-title").forEach(inp=>
        inp.onfocus=()=>{lastRow=+inp.closest(".aprow").dataset.i;});
      const setExe=v=>{const r=rows()[lastRow]||rows()[0];
        if(r)r.querySelector(".ap-exe").value=v;};
      el("ap-proc").onchange=e=>{if(e.target.value)setExe(e.target.value);};
      on("ap-refresh",fill);
      on("ap-browse",async()=>{toast("opening file picker…");
        const j=await(await fetch("/api/browse-exe")).json();
        if(j.path)setExe(j.path.split(/[\\/]/).pop());});
      on("ap-add",async()=>{
        // Persist what is on screen first, so adding a row never discards an
        // edit that had not been saved yet. The new row is counted rather than
        // stored: an empty trigger is not a rule, so saving one would be
        // filtered straight back out and the button would look dead.
        await patchProfile({triggers:read()},false);
        apExtra++; draw();});
      root.querySelectorAll("[data-aprm]").forEach(b=>b.onclick=async()=>{
        const keep=read().filter((_,i)=>i!==+b.dataset.aprm);
        await patchProfile({triggers:keep},false);
        apExtra=0; toast("app removed");draw();});
      on("ap-save",async()=>{
        const t=read();
        await patchProfile({triggers:t},false);
        apExtra=0;
        toast(t.length?`${t.length} app${t.length>1?"s":""} bound`:"triggers cleared");
        draw();});
      on("ap-clear",async()=>{
        await patchProfile({triggers:[]},false);
        apExtra=0; toast("triggers cleared");draw();});
    }
  }

  if(tab==="Other Settings"){
    on("fw-open",()=>{window.open(FIRMWARE_URL,"_blank","noopener");});
    on("prof-export",async()=>{
      const r=await post("/api/profile/export",{name:editing});
      toast(r.saved?`exported ${editing}`:"export cancelled");});
    on("reset-keys",async()=>{
      if(!confirm("Restore the key bindings to factory defaults?"))return;
      toast("not wired to a command yet — use Restore factory settings",true);});
    on("factory",async()=>{
      if(!confirm("Restore factory settings?\\n\\nThis wipes the keyboard's onboard "+
        "settings. A snapshot is saved to backups/ first."))return;
      if(prompt("Type ERASE to confirm:")!=="ERASE"){toast("cancelled");return;}
      const r=await post("/api/factory-reset",{confirm:"ERASE"});
      toast("factory reset done — snapshot in "+(r.backup||"backups/"));});
  }
}

/* ---- macro recording in the browser ------------------------------------ */
addEventListener("keydown",e=>{
  if(!recording||tab!=="Macro Setting")return;
  e.preventDefault();
  const now=performance.now(),dt=Math.round(now-recLast);recLast=now;
  if(macroSteps.length&&dt>5)macroSteps.push({kind:"delay",delay_ms:dt});
  const kc=parseKey(e.key.length===1?e.key.toUpperCase():e.key);
  if(kc!==null)macroSteps.push({kind:"down",keycode:kc});
  draw();
});
addEventListener("keyup",e=>{
  if(!recording||tab!=="Macro Setting")return;
  e.preventDefault();
  const now=performance.now(),dt=Math.round(now-recLast);recLast=now;
  if(dt>5)macroSteps.push({kind:"delay",delay_ms:dt});
  const kc=parseKey(e.key.length===1?e.key.toUpperCase():e.key);
  if(kc!==null)macroSteps.push({kind:"up",keycode:kc});
  draw();
});

/* ---- sidebar actions ---------------------------------------------------- */
$("#p-add").onclick=async()=>{
  const n=await askText("New profile","Name","","Create");
  if(!n)return;
  try{await post("/api/profile/create-blank",{name:n});}catch(e){return;}
  toast(`created ${n}`);await refreshProfiles();openProfile(n);};
$("#p-onboard").onclick=async()=>{
  if(!editing){toast("pick a profile first",true);return;}
  if(!confirm(`Save "${editing}" into the keyboard's onboard memory?\n\n`+
    "This is what the keyboard uses on its own with this app closed. It costs "+
    "one flash write cycle."))return;
  toast("writing to the keyboard…");
  await post("/api/profile/set-onboard",{name:editing});
  toast(`${editing} saved to the keyboard`);await refreshProfiles();draw();};
$("#p-import").onclick=async()=>{
  const r=await post("/api/profile/import",{});
  if(!r.imported.length&&!r.skipped.length){toast("import cancelled");return;}
  if(r.skipped.length)toast(`skipped: ${r.skipped.join(", ")}`,true);
  if(r.imported.length){
    toast(`imported ${r.imported.join(", ")}`);
    await refreshProfiles(); openProfile(r.imported[0]);
  }};
$("#nav-equipment").onclick=()=>setView("equipment");
$("#nav-macros").onclick=()=>setView("macros");
$("#open-settings").onclick=openSettings;

function openSettings(){
  $("#modal").innerHTML=`<div class="modal"><div class="box" style="max-width:560px">
    <h2>Settings</h2>
    <div class="row" style="margin-top:16px">
      <label class="f"><input type="checkbox" id="set-startup"> Start when I sign in</label></div>
    <div class="row" style="margin-top:10px">
      <label class="f"><input type="checkbox" id="set-updates"> Check for updates on startup</label></div>
    <div class="row" style="margin-top:18px">
      <button id="set-check">Check for updates now</button>
      <span class="sub" id="set-ver"></span></div>

    <h3 style="margin:26px 0 8px">Profiles</h3>
    <div class="row">
      <button id="set-export-all">Export all profiles…</button>
      <button id="set-import">Import profiles…</button></div>
    <div class="sub" style="margin-top:8px">
      App triggers are not carried across, so an imported profile never steals
      another one's app.</div>

    <div class="row" style="margin-top:24px">
      <span class="spacer"></span>
      <button class="primary" id="set-close">Done</button></div>
  </div></div>`;
  const m=$("#modal"), q=i=>m.querySelector("#"+i);
  fetch("/api/settings").then(r=>r.json()).then(s2=>{
    q("set-startup").checked=!!s2.start_at_login;
    q("set-updates").checked=!!s2.check_updates;
    q("set-ver").textContent=`version ${s2.version}`;});
  q("set-startup").onchange=async()=>{
    const r=await post("/api/settings/save",{start_at_login:q("set-startup").checked});
    // The registry decides, so reflect what actually happened.
    q("set-startup").checked=r.start_at_login;
    toast(r.start_at_login?"will start at sign-in":"will not start at sign-in");};
  q("set-updates").onchange=async()=>{
    await post("/api/settings/save",{check_updates:q("set-updates").checked});
    toast(q("set-updates").checked?"update check on":"update check off");};
  q("set-check").onclick=async()=>{
    q("set-check").disabled=true; q("set-ver").textContent="checking…";
    const u=await(await fetch("/api/update-check")).json();
    q("set-check").disabled=false;
    q("set-ver").textContent=u.checked
      ? (u.update?`update available: ${u.latest}`:`up to date (${u.current})`)
      : "could not reach GitHub";
    if(u.update)showUpdate(u);};
  q("set-export-all").onclick=async()=>{
    const r=await post("/api/profile/export",{});
    toast(r.saved?`exported ${r.count} profile(s)`:"export cancelled");};
  q("set-import").onclick=async()=>{
    const r=await post("/api/profile/import",{});
    if(r.skipped.length)toast(`skipped: ${r.skipped.join(", ")}`,true);
    if(r.imported.length){toast(`imported ${r.imported.length} profile(s)`);
      await refreshProfiles();draw();}
    else if(!r.skipped.length)toast("import cancelled");};
  q("set-close").onclick=()=>{m.innerHTML="";};
}

function showUpdate(u){
  $("#modal").innerHTML=`<div class="modal"><div class="box" style="max-width:520px">
    <h2>Update available</h2>
    <div style="margin-top:12px">Version <b>${u.latest}</b> is out.
      You have ${u.current}.</div>
    ${u.notes?`<div class="sub" style="margin-top:12px; white-space:pre-wrap; max-height:220px; overflow:auto">${u.notes}</div>`:""}
    <div class="row" style="margin-top:20px"><span class="spacer"></span>
      <button id="up-later">Later</button>
      <a class="btn primary" href="${u.url}" target="_blank" rel="noopener">Open releases</a></div>
  </div></div>`;
  $("#modal").querySelector("#up-later").onclick=()=>{$("#modal").innerHTML="";};
}

/* Startup check. Silent unless there is something to report -- an "up to date"
   popup on every launch is noise. */
(async()=>{try{
  const s2=await(await fetch("/api/settings")).json();
  if(!s2.check_updates)return;
  const u=await(await fetch("/api/update-check")).json();
  if(u.checked&&u.update)showUpdate(u);
}catch(e){}})();

/* Small in-app dialogs. The browser's prompt and confirm are modal to the
   whole window, look nothing like the app, and cannot be styled. */
function askText(title,label,value,okLabel){
  return new Promise(resolve=>{
    $("#modal").innerHTML=`<div class="modal"><div class="box" style="max-width:420px">
      <h2>${title}</h2>
      <label class="f" style="display:block; margin-top:14px">${label}
        <input type="text" id="ask-input" value="${(value||"").replace(/"/g,"&quot;")}"
               style="width:100%; margin-top:8px"></label>
      <div class="row" style="margin-top:20px"><span class="spacer"></span>
        <button id="ask-cancel">Cancel</button>
        <button class="primary" id="ask-ok">${okLabel||"OK"}</button></div>
    </div></div>`;
    const m=$("#modal"), input=m.querySelector("#ask-input");
    const done=v=>{m.innerHTML="";resolve(v);};
    input.focus(); input.select();
    input.onkeydown=e=>{
      if(e.key==="Enter"){e.preventDefault();done(input.value.trim()||null);}
      if(e.key==="Escape"){e.preventDefault();done(null);}};
    m.querySelector("#ask-cancel").onclick=()=>done(null);
    m.querySelector("#ask-ok").onclick=()=>done(input.value.trim()||null);
  });
}
function askConfirm(title,body,okLabel){
  return new Promise(resolve=>{
    $("#modal").innerHTML=`<div class="modal"><div class="box" style="max-width:420px">
      <h2>${title}</h2>
      <div class="sub" style="margin-top:12px">${body}</div>
      <div class="row" style="margin-top:20px"><span class="spacer"></span>
        <button id="cf-cancel">Cancel</button>
        <button class="danger" id="cf-ok">${okLabel||"Delete"}</button></div>
    </div></div>`;
    const m=$("#modal");
    const done=v=>{m.innerHTML="";resolve(v);};
    m.querySelector("#cf-cancel").onclick=()=>done(false);
    m.querySelector("#cf-ok").onclick=()=>done(true);
  });
}

function closeCtxMenu(){ $("#ctxmenu").classList.remove("open"); }

function openProfileMenu(name,x,y){
  const m=$("#ctxmenu");
  m.innerHTML=`<button data-act="rename">Rename…</button>
    <button data-act="duplicate">Duplicate…</button>
    <button data-act="delete" class="danger">Delete…</button>`;
  m.classList.add("open");
  // Placed after it is measurable, then pulled back inside the viewport.
  const r=m.getBoundingClientRect();
  m.style.left=Math.min(x,innerWidth-r.width-8)+"px";
  m.style.top=Math.min(y,innerHeight-r.height-8)+"px";
  m.querySelectorAll("[data-act]").forEach(b=>b.onclick=async()=>{
    closeCtxMenu();
    const act=b.dataset.act;
    try{
      if(act==="rename"){
        const nn=await askText("Rename profile","New name",name,"Rename");
        if(!nn||nn===name)return;
        await post("/api/profile/rename",{name,new_name:nn});
        if(editing===name)editing=nn;
        toast(`renamed to ${nn}`);
      }else if(act==="duplicate"){
        const nn=await askText("Duplicate profile","Name for the copy",
                               name+" copy","Duplicate");
        if(!nn)return;
        await post("/api/profile/duplicate",{name,new_name:nn});
        toast(`created ${nn}`);
      }else if(act==="delete"){
        const ok=await askConfirm("Delete profile",
          `<b>${name}</b> will be removed permanently. Its settings stay on the `+
          `keyboard until another profile is applied.`,"Delete");
        if(!ok)return;
        await post("/api/profile/delete",{name});
        if(editing===name){editing=null;editDoc=null;}
        toast(`deleted ${name}`);
      }
      await refreshProfiles();draw();
    }catch(e){}
  });
}

addEventListener("contextmenu",e=>{
  const row=e.target.closest&&e.target.closest("#plist [data-p]");
  if(!row)return;
  e.preventDefault();
  openProfileMenu(row.dataset.p,e.clientX,e.clientY);
});
addEventListener("keydown",e=>{if(e.key==="Escape")closeCtxMenu();});
addEventListener("scroll",closeCtxMenu,true);

async function tick(){
  try{latest=await(await fetch("/api/state")).json();
    drawSidebar();paintGauge();}
  catch(e){$("#conn").textContent="driver not reachable";}
  setTimeout(tick,60);
}
tick();
Promise.all([refreshProfiles(),loadConfig()]).then(()=>{
  if(!editing&&profiles.length)openProfile(profiles[0].name); else draw();});
</script>
</body></html>
"""
