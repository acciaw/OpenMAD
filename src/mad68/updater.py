"""Download and install a new release from inside the app.

The old flow opened the GitHub releases page and left the user to find the
right file, run it and restart the app. This does the same three steps without
leaving the HUD.

Shape of it:

    check_for_update()      -> is there a newer release, and which asset is it
    Updater.start(asset)    -> stream it to %TEMP%, reporting progress
    Updater.install()       -> run the installer silently, then quit so it can
                               replace the executable we are running from

The last step is the awkward one. Windows will not overwrite a running .exe, so
the app has to be gone before the installer reaches the file-copy stage. The
installer is therefore spawned detached, and the app shuts itself down straight
after; packaging/mad68.iss relaunches it when a silent install finishes.

What is trusted here, and what is not: the release metadata comes from the
GitHub API over TLS, the download must stay on GitHub's own hosts across every
redirect, and the bytes are checked against the size and (when the API gives
one) the SHA-256 the API reported before anything is executed. A download that
fails any of those is deleted rather than run.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# Every host a GitHub release download legitimately passes through. Release
# assets 302 away from github.com to a CDN, so the check has to be applied to
# the final URL as well as the first, or the allow-list proves nothing.
_ALLOWED_HOST_SUFFIXES = (".githubusercontent.com", ".github.com")
_ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com"}

# The installer Inno Setup produces: OpenMAD-Setup-1.2.3.exe. Anything else in
# a release (source archives, a portable exe, checksums) is not what we run.
_INSTALLER_RE = re.compile(r"^[\w.-]+-Setup-[\d.]+\.exe$", re.IGNORECASE)

_CHUNK = 64 * 1024


def _host_allowed(url: str) -> bool:
    try:
        parts = urlparse(url)
    except Exception:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIXES)


def pick_installer_asset(assets: list) -> dict | None:
    """The release asset that is our installer, or None if the release has none.

    A release with no installer attached is a normal thing to meet -- a tag
    pushed without a build, or a source-only release -- and it means "cannot
    update from here", not an error.
    """
    for a in assets or []:
        name = a.get("name") or ""
        url = a.get("browser_download_url") or ""
        if _INSTALLER_RE.match(name) and _host_allowed(url):
            return {
                "name": name,
                "url": url,
                "size": int(a.get("size") or 0),
                # Present on newer GitHub API responses as "sha256:<hex>".
                "digest": (a.get("digest") or ""),
            }
    return None


def _download_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "OpenMAD-update"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Updater:
    """Holds one download at a time, and the state the HUD polls for.

    Deliberately a single shared instance rather than a job per request: there
    is only ever one update in flight, and a second click on Install must join
    the download already running instead of starting a competing one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"          # idle | downloading | ready | installing | error
        self.error = ""
        self.done = 0
        self.total = 0
        self.path: Path | None = None
        self.version = ""
        self._thread: threading.Thread | None = None

    # state

    def snapshot(self) -> dict:
        with self._lock:
            pct = int(self.done * 100 / self.total) if self.total else 0
            return {
                "state": self.state,
                "error": self.error,
                "done": self.done,
                "total": self.total,
                "percent": pct,
                "version": self.version,
                "file": self.path.name if self.path else "",
            }

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    # download

    def start(self, asset: dict, version: str = "") -> dict:
        """Begin downloading, unless a download is already running or finished."""
        with self._lock:
            if self.state in ("downloading", "installing"):
                return self.snapshot_locked()
            if self.state == "ready" and self.path and self.path.exists():
                return self.snapshot_locked()
            self.state = "downloading"
            self.error = ""
            self.done = 0
            self.total = int(asset.get("size") or 0)
            self.version = version
            self.path = None
        self._thread = threading.Thread(
            target=self._run, args=(asset,), daemon=True, name="update-download")
        self._thread.start()
        return self.snapshot()

    def snapshot_locked(self) -> dict:
        pct = int(self.done * 100 / self.total) if self.total else 0
        return {"state": self.state, "error": self.error, "done": self.done,
                "total": self.total, "percent": pct, "version": self.version,
                "file": self.path.name if self.path else ""}

    def _run(self, asset: dict) -> None:
        try:
            path = self._download(asset)
        except Exception as exc:
            self._set(state="error", error=str(exc))
            return
        self._set(state="ready", path=path)

    def _download(self, asset: dict) -> Path:
        url = asset.get("url") or ""
        if not _host_allowed(url):
            raise ValueError(f"refusing to download from {url!r}")

        name = os.path.basename(asset.get("name") or "OpenMAD-Setup.exe")
        if not _INSTALLER_RE.match(name):
            raise ValueError(f"unexpected installer name {name!r}")
        dest = _download_dir() / name
        part = dest.with_suffix(dest.suffix + ".part")

        req = urllib.request.Request(url, headers={"User-Agent": "mad68-driver"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=30) as r:
            # Re-check after redirects: the allow-list is meaningless if only
            # the URL we started with was ever validated.
            final = getattr(r, "url", url)
            if not _host_allowed(final):
                raise ValueError(f"download redirected off GitHub to {final!r}")
            total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
            self._set(total=total)
            done = 0
            with part.open("wb") as fh:
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self._set(done=done)

        expected = int(asset.get("size") or 0)
        if expected and done != expected:
            part.unlink(missing_ok=True)
            raise ValueError(f"download is {done} bytes, expected {expected}")

        want = (asset.get("digest") or "").strip().lower()
        if want.startswith("sha256:"):
            got = digest.hexdigest()
            if got != want.split(":", 1)[1]:
                part.unlink(missing_ok=True)
                raise ValueError("downloaded file failed its SHA-256 check")

        dest.unlink(missing_ok=True)
        part.replace(dest)
        return dest

    # install

    def install(self, on_exit=None) -> dict:
        """Run the downloaded installer, then hand back so the app can quit.

        /SILENT rather than /VERYSILENT: the app is about to vanish, and Inno's
        own progress window is the only sign left that something is happening.
        """
        with self._lock:
            if self.state != "ready" or not self.path or not self.path.exists():
                raise ValueError("no downloaded update to install")
            installer = self.path
            self.state = "installing"

        log = _download_dir() / "install.log"
        try:
            subprocess.Popen(
                [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                 f"/LOG={log}"],
                creationflags=(subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP),
                close_fds=True,
            )
        except Exception as exc:
            self._set(state="error", error=f"could not start the installer: {exc}")
            raise

        # Quitting is the caller's job, and it has to happen: the installer
        # cannot replace OpenMAD.exe while this process still has it open.
        if on_exit is not None:
            threading.Timer(1.0, on_exit).start()
        return {"installing": True, "log": str(log)}


# One per process, shared by every request handler.
UPDATER = Updater()


def frozen_install_supported() -> tuple[bool, str]:
    """Whether installing from here can work at all, and why not when it cannot.

    From a source checkout there is no installer to run and nothing to replace,
    so the HUD should say so rather than offer a button that cannot work.
    """
    if not getattr(sys, "frozen", False):
        return False, ("Running from a source checkout, so there is nothing for an "
                       "installer to replace. Update with git instead.")
    return True, ""
