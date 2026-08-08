#!/usr/bin/env python3
"""OpenMAD system-tray app for the MAD68 HE keyboard.

Runs the app-aware profile switcher in the background and exposes it from the
tray: current profile, manual override, auto-switch toggle, and a live view of
what the foreground window resolved to.

    python tools/tray.py                # normal
    python tools/tray.py --dry-run      # watch and log decisions, write nothing

The icon comes from assets/icon.ico when present. A corner dot marks the
switcher's state: none when auto-switching is on, amber when paused, red on
error.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from mad68.paths import assets_dir, data_dir, seed_user_data  # noqa: E402
from mad68.switcher import (  # noqa: E402
    SwitchEvent,
    Switcher,
    SwitcherConfig,
    foreground_app,
)

# Bundled resources and the user's data are different places in a packaged
# build; see mad68.paths. Writing the config beside the resources put it in a
# directory PyInstaller deletes on exit, which is why no setting survived a
# reboot.
DATA_DIR = data_dir()
PROFILE_DIR = seed_user_data()
CONFIG_PATH = DATA_DIR / "switcher.json"
LOG_PATH = DATA_DIR / "switcher.log"
HUD_PORT = 8787

def server_is_current(port: int) -> bool:
    """True only if our HUD is on that port AND running the same code we are."""
    try:
        import json as _json
        import urllib.request
        from mad68.hud import build_stamp
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/version", timeout=1.5) as r:
            remote = _json.loads(r.read())
        return (remote.get("app") == "openmad-hud"
                and remote.get("build") == build_stamp()["build"])
    except Exception:
        return False


def stop_stale_server(port: int) -> None:
    """Shut down an out-of-date HUD holding the port, so a fresh one can bind."""
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown",
                                     data=b"{}",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
        print(f"replaced an out-of-date HUD on port {port}")
    except Exception:
        pass
    # Wait for the socket to be released, but not for long: this used to allow
    # twenty rounds of a fifth of a second each, which is most of the delay
    # people saw when opening the configurator.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                time.sleep(0.1)
        except OSError:
            return


COLOURS = {
    "active": (56, 142, 60),   # green  - auto-switching on
    "paused": (245, 152, 32),  # amber  - paused
    "error": (198, 40, 40),    # red    - last action failed
}


# Drop an icon.ico here and the tray uses it. Loaded once and cached, because
# pystray re-reads the icon on every state change. A resource, not user data:
# it ships inside the executable.
ICON_PATH = assets_dir() / "icon.ico"
_icon_cache: dict[str, Image.Image] = {}


def make_icon(state: str) -> Image.Image:
    """The tray icon, tinted by a small dot for the switcher's state.

    Uses assets/icon.ico when present. The profile's initial is deliberately
    not drawn on it, a tray icon should be recognisably the app, and the
    active profile is already named in the tooltip and the menu.
    """
    if state in _icon_cache:
        return _icon_cache[state]

    size = 64
    if ICON_PATH.exists():
        try:
            img = Image.open(ICON_PATH).convert("RGBA").resize(
                (size, size), Image.LANCZOS)
        except Exception:
            img = None
    else:
        img = None

    if img is None:
        # Placeholder until an icon.ico is supplied: a plain accent tile, no
        # lettering.
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle(
            [2, 2, size - 3, size - 3], radius=14, fill=COLOURS["active"])

    # State dot in the corner, so paused/error is visible without a tooltip.
    if state != "active":
        d = ImageDraw.Draw(img)
        r = 11
        d.ellipse([size - 2 * r - 3, size - 2 * r - 3, size - 3, size - 3],
                  fill=COLOURS.get(state, COLOURS["paused"]),
                  outline=(0, 0, 0, 190), width=2)

    _icon_cache[state] = img
    return img


class TrayApp:
    def __init__(self, dry_run: bool = False):
        self._cfg_mtime = 0.0
        self.config = self._load_config()
        self.state = "active" if self.config.enabled else "paused"
        self.last_note = ""
        self.manual_override: str | None = None

        self.switcher = Switcher(
            self.config, PROFILE_DIR, on_event=self._on_event, dry_run=dry_run
        )
        self.dry_run = dry_run
        # The configurator's HTTP server, served from this process.
        self._httpd = None
        self._sampler = None
        self.icon = pystray.Icon(
            "mad68",
            make_icon(self.state),
            "OpenMAD",
            menu=self._menu(),
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # config

    def _load_config(self) -> SwitcherConfig:
        if CONFIG_PATH.exists():
            try:
                cfg = SwitcherConfig.load(CONFIG_PATH)
                self._cfg_mtime = self._config_mtime()
                return cfg
            except Exception as exc:
                # Deliberately NOT overwritten with defaults: a file we could
                # not parse may still be the user's only copy of their rules.
                print(f"config unreadable ({exc}); using defaults", file=sys.stderr)
                return SwitcherConfig()
        cfg = SwitcherConfig()
        cfg.save(CONFIG_PATH)
        self._cfg_mtime = self._config_mtime()
        return cfg

    @staticmethod
    def _config_mtime() -> float:
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    def _save_config(self) -> None:
        """Persist only the settings the tray owns.

        enabled is the tray's; rules and default_profile belong to the
        HUD, which writes the same file. The tray's copy is a snapshot taken at
        startup, so saving it wholesale reverted every HUD edit made since, toggling auto-switch was enough to delete an app rule added minutes
        earlier, which then looked like auto-switching simply not working.
        Re-read, change the one field, write back.
        """
        try:
            on_disk = (SwitcherConfig.load(CONFIG_PATH) if CONFIG_PATH.exists()
                       else SwitcherConfig())
        except Exception as exc:
            # The file is there but will not parse. self.config is whatever
            # _load_config fell back to, which is the defaults -- writing it
            # here would replace the user's only copy of their rules with an
            # empty one, the exact thing _load_config refuses to do. Toggling
            # auto-switch was enough to trigger it. Keep the file instead; the
            # one setting this method owns is not worth losing the rest for.
            print(f"config unreadable ({exc}); leaving it untouched rather than "
                  f"overwriting it with defaults", file=sys.stderr)
            return
        on_disk.enabled = self.config.enabled
        try:
            on_disk.save(CONFIG_PATH)
        except Exception as exc:
            print(f"could not save config: {exc}", file=sys.stderr)
            return
        # Adopt whatever else was on disk, so the in-memory copy the switcher
        # holds a reference to matches the file.
        self.config.rules = on_disk.rules
        self.config.default_profile = on_disk.default_profile
        self._cfg_mtime = self._config_mtime()

    # events

    def _on_event(self, event: SwitchEvent) -> None:
        if "failed" in event.note or "not found" in event.note or "unreadable" in event.note:
            self.state = "error"
        elif self.config.enabled:
            self.state = "active"
        self.last_note = event.note

        if event.writes or event.note:
            line = (
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  "
                f"app={event.app.exe if event.app else '-':<24} "
                f"profile={event.profile:<12} writes={event.writes:<4} {event.note}"
            )
            print(line)
            try:
                with LOG_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass  # logging must never take the tray down

        self._refresh()

    def _refresh(self) -> None:
        name = self.switcher.active_profile or "?"
        self.icon.icon = make_icon(self.state)
        app = self.switcher.current_app
        parts = [f"OpenMAD — {name}"]
        if self.dry_run:
            parts.append("(dry run)")
        if app and app.exe:
            parts.append(f"fg: {app.exe}")
        if not self.config.enabled:
            parts.append("auto-switch paused")
        if self.last_note:
            parts.append(self.last_note)
        self.icon.title = "\n".join(parts)[:127]  # Windows tooltip limit
        self.icon.menu = self._menu()
        try:
            # Required for a replaced menu to take effect on some backends.
            self.icon.update_menu()
        except Exception:
            pass  # a cosmetic refresh must never take the tray down

    # menu

    def _menu(self) -> pystray.Menu:
        items: list = []

        active = self.switcher.active_profile or "unknown"
        items.append(pystray.MenuItem(f"Active: {active}", None, enabled=False))
        app = self.switcher.current_app
        if app and app.exe:
            items.append(pystray.MenuItem(f"Foreground: {app.exe}", None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)

        items.append(
            pystray.MenuItem(
                "Auto-switch",
                self._toggle_auto,
                checked=lambda _i: self.config.enabled,
            )
        )
        items.append(pystray.Menu.SEPARATOR)

        for name in self.switcher.available_profiles():
            items.append(
                pystray.MenuItem(
                    f"Apply '{name}'",
                    self._make_apply(name),
                    checked=lambda _i, n=name: self.switcher.active_profile == n,
                    radio=True,
                )
            )

        # Only offered when the active profile actually carries key bindings.
        # Switching profiles never applies them, since that is the one setting
        # that cannot be written without spending the keyboard's permanent
        # memory, so this is the way to do it without opening the configurator.
        if self._active_profile_has_keymap():
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Apply this profile's key bindings",
                                          self._apply_keymap))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Open", self._open_hud, default=True))
        items.append(pystray.MenuItem("Re-read config", self._reload_config))
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Menu(*items)

    def _active_profile_has_keymap(self) -> bool:
        """Whether the active profile stores key bindings of its own.

        Deliberately a file read and nothing more. Comparing against the board
        would need a device round trip, and this runs every time the menu is
        built.
        """
        name = self.switcher.active_profile
        if not name:
            return False
        path = PROFILE_DIR / f"{name}.json"
        try:
            import json as _json
            doc = _json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return False
        return bool(doc.get("keymap_hex"))

    def _apply_keymap(self, _icon=None, _item=None) -> None:
        """Write the active profile's key bindings to the keyboard."""
        name = self.switcher.active_profile
        if not name:
            return

        def run() -> None:
            try:
                from mad68.device import Mad68
                from mad68.profile import Profile
                prof = Profile.load(PROFILE_DIR / f"{name}.json")
                with Mad68(writes=True) as kb:
                    written = prof.apply_keymap(kb)
                self.last_note = (f"key bindings applied from '{name}'" if written
                                  else f"'{name}' key bindings already active")
            except Exception as exc:
                self.state = "error"
                self.last_note = f"could not apply key bindings: {exc}"
            self._refresh()

        threading.Thread(target=run, daemon=True).start()

    def _open_hud(self, _icon=None, _item=None) -> None:
        """Launch the local server if needed, then open the home page.

        Marked as the menu default, so a double-click on the tray icon opens it
        too. The home page detects the keyboard and links through to the HUD.

        Safe to run alongside the switcher: Windows delivers input reports to
        every open HID handle, and reply matching is channel- and sub-command
        aware, so the two processes do not confuse each other's replies.
        """
        url = f"http://127.0.0.1:{HUD_PORT}/"

        def go() -> None:
            # Already serving from this process: nothing to start, open at once.
            if self._httpd is not None:
                webbrowser.open(url)
                return

            # "Something is listening" is not good enough. A server started
            # before the code changed keeps its page and settings in memory for
            # as long as it runs, so reusing it silently serves stale UI and
            # stale configuration. Compare build stamps and replace it if it
            # does not match what this process would produce.
            if server_is_current(HUD_PORT):
                webbrowser.open(url)
                self.last_note = f"configurator at {url}"
                self._refresh()
                return

            self.last_note = "starting the configurator..."
            self._refresh()
            try:
                self.icon.notify("Starting the configurator...", "OpenMAD")
            except Exception:
                # Balloon notifications are not available on every desktop.
                pass
            stop_stale_server(HUD_PORT)

            # Served from this process rather than a second one. Spawning a
            # fresh interpreter meant paying for process creation, importing
            # everything again and opening the device again before the port
            # even bound, which is where the five to ten second wait came from.
            # Everything needed is already loaded here.
            try:
                from mad68.hud import serve
                httpd, sampler = serve(PROFILE_DIR, port=HUD_PORT)
            except Exception as exc:
                self.state = "error"
                self.last_note = f"could not start the configurator: {exc}"
                self._refresh()
                return

            self._httpd, self._sampler = httpd, sampler
            threading.Thread(target=httpd.serve_forever, daemon=True,
                             name="hud-server").start()
            webbrowser.open(url)
            self.last_note = f"configurator at {url}"
            self._refresh()

        threading.Thread(target=go, daemon=True).start()

    def _make_apply(self, name: str):
        def handler(_icon=None, _item=None) -> None:
            # A manual pick pauses auto-switching, otherwise the next poll would
            # immediately undo the user's choice, which would read as a bug.
            if self.config.enabled:
                self.config.enabled = False
                self.state = "paused"
                self.last_note = f"auto-switch paused by manual pick of '{name}'"
                self._save_config()
            self.manual_override = name
            threading.Thread(
                target=lambda: self.switcher.apply_profile(name, force=True), daemon=True
            ).start()

        return handler

    def _toggle_auto(self, _icon=None, _item=None) -> None:
        self.config.enabled = not self.config.enabled
        self.state = "active" if self.config.enabled else "paused"
        self.last_note = "" if self.config.enabled else "auto-switch paused"
        self._save_config()
        self._refresh()

    def _reload_config(self, _icon=None, _item=None) -> None:
        self.config = self._load_config()
        self.switcher.config = self.config
        self.state = "active" if self.config.enabled else "paused"
        self.last_note = "config reloaded"
        self._refresh()

    def _quit(self, _icon=None, _item=None) -> None:
        self._stop.set()
        self.switcher.stop()
        # Only tear down a configurator this tray started; one launched
        # separately is the user's to close.
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
        if self._sampler is not None:
            try:
                self._sampler.stop()
            except Exception:
                pass
        self.icon.stop()

    # run

    def _loop(self) -> None:
        # Establish the starting profile once so the first real switch has a
        # baseline to compare against.
        try:
            app = foreground_app()
            self.switcher.current_app = app
            want = self.config.resolve(app)
            # None means no rule matched and no default is set, leave the
            # keyboard on whatever it already has.
            if want is not None:
                self.switcher.apply_profile(want)
        except Exception as exc:
            self.state = "error"
            self.last_note = f"startup: {exc}"
            self._refresh()

        while not self._stop.is_set():
            try:
                self._reload_if_changed()
                self.switcher.poll_once()
            except Exception as exc:
                self.state = "error"
                self.last_note = f"poll: {exc}"
                self._refresh()
            self._stop.wait(self.config.poll_interval_ms / 1000)

    def _reload_if_changed(self) -> None:
        """Pick up edits the HUD made to switcher.json.

        Binding an app in the HUD writes the rule straight to disk, but the
        tray was only re-reading the file when "Reload config" was clicked by
        hand. So a trigger set while the tray was running did nothing, and the
        obvious conclusion was that auto-switching was broken.
        """
        mtime = self._config_mtime()
        if not mtime or mtime == self._cfg_mtime:
            return
        try:
            fresh = SwitcherConfig.load(CONFIG_PATH)
        except Exception:
            self._cfg_mtime = mtime  # don't retry a bad file every poll
            return
        self._cfg_mtime = mtime
        # Mutated in place: the Switcher holds a reference to this object.
        self.config.rules = fresh.rules
        self.config.default_profile = fresh.default_profile
        self.config.poll_interval_ms = fresh.poll_interval_ms
        self.config.debounce_ms = fresh.debounce_ms
        self.config.min_dwell_ms = fresh.min_dwell_ms
        # enabled is the tray's own switch, and pausing from the menu must not
        # be undone by an unrelated HUD write.
        self.last_note = "config reloaded"
        self._refresh()

    def _setup(self, icon: pystray.Icon) -> None:
        """pystray setup callback.

        Passing a custom setup= to Icon.run() REPLACES pystray's default
        handler, and that default is the only thing that sets visible = True
        (see pystray/_base.py _start_setup). Forgetting it here means the
        background thread runs happily while no icon ever appears in the tray.
        """
        icon.visible = True
        print("tray icon visible; watching foreground window")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def run(self) -> None:
        self.icon.run(setup=self._setup)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="log switching decisions without writing to the keyboard")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("the tray app requires Windows", file=sys.stderr)
        return 2

    print(f"profiles: {PROFILE_DIR}")
    print(f"config:   {CONFIG_PATH}")
    print(f"log:      {LOG_PATH}")
    if args.dry_run:
        print("DRY RUN: no writes will be issued")
    TrayApp(dry_run=args.dry_run).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
