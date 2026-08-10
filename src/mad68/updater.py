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
the app has to be gone before the installer reaches the file-copy stage -- and
Inno gets there about two tenths of a second after it starts, which is long
before a shutdown finishes.

So the app does not launch the installer at all. It writes a small batch
launcher, hands it the installer path and its own PID, and starts it in the
background; the launcher waits for that PID to disappear and only then runs the
installer. The app then shuts itself down. packaging/mad68.iss relaunches it
when the silent install finishes.

Progress is recorded in two files next to the download: launcher.log (did the
launcher run, did it see the app exit, what did the installer return) and
install.log (Inno's own). The failure this replaced was silent in both, which
is why the first of those exists.

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
import time
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

# The launcher that actually starts the installer, and how it is built.
#
# It is a file on disk with no paths baked into it: the installer, the log and
# the PID to wait for all arrive as arguments. That is not tidiness, it is the
# whole point.
#
#   Quoting. subprocess quotes list arguments the way the C runtime parses
#   them, so a quote inside an argument is escaped as \". cmd.exe does not use
#   those rules -- it takes the backslash literally. Building one long command
#   string and handing it to `cmd /c` therefore produced
#       '\"C:\...\OpenMAD-Setup-1.1.2.exe\"' is not recognized as an internal
#       or external command
#   and the installer never ran, on every machine, regardless of the path. The
#   app quit anyway, because quitting is on a timer that does not know whether
#   the launch worked -- so from outside it looked like "it downloads the
#   update and then just closes itself". Arguments passed as separate list
#   entries are quoted plainly and survive intact.
#
#   Encoding. A batch file is read in the console's OEM code page, so a path
#   with a non-ASCII character in it (a user name with an accent) would be
#   mangled if it were written into the script. As an argument it never is.
#
#   Expansion. `%` is legal in a Windows path and cmd expands it even inside
#   quotes. Arguments are not re-expanded, so that cannot bite either.
#
# The wait is on the app's PID rather than a fixed sleep. Windows will not
# overwrite a running executable, and Inno reaches its in-use check about two
# tenths of a second after launch, so the installer must not start before the
# app is gone -- but a sleep long enough to be safe is also a sleep the user
# sits through, and any fixed number is wrong for a shutdown that stalls.
_LAUNCHER_NAME = "install-update.cmd"

# ~1s per iteration (ping -n 2). The cap is a backstop for an app that never
# exits; CloseApplications=force in packaging/mad68.iss is the one after that.
_LAUNCHER_MAX_TRIES = 120

# How long to wait for the launcher to prove it is running before giving up and
# leaving the app open. It writes its log immediately, so this only has to
# cover process creation.
_LAUNCH_CONFIRM_S = 5.0

# Pause between the installer finishing and the new build being started.
#
# The relaunch used to be Inno's job, an `[Run] ... Check: WizardSilent` entry
# in packaging/mad68.iss. Inno logged
#
#     17:29:52.874  Installation process succeeded.
#     17:29:52.880  -- Run entry --  Type: Exec
#     17:29:53.440  Log closed.
#
# so the new executable was started six milliseconds after a 19 MB file
# finished being written, by a process that then exited half a second later.
# The PyInstaller onefile bootloader unpacks that archive into %TEMP%\_MEInnnn
# before it can run, and it did not get there: it failed with "Failed to load
# Python DLL ... python313.dll. LoadLibrary: The specified module could not be
# found", i.e. its own extraction was incomplete. Starting the same executable
# by hand a few seconds later worked every time, and %TEMP% still holds an
# orphaned _MEI directory with 20 of the 63 files in it from an earlier
# occurrence.
#
# So the relaunch happens here instead, after the installer has fully exited
# and after a pause, which is the one variable that distinguished the failing
# launch from the working one.
_RESTART_SETTLE_S = 4

# `ping` rather than `timeout`, which needs a console this deliberately lacks.
# `tasklist | find` is a pipe between two console programs, which is why the
# launch below uses CREATE_NO_WINDOW (a console, just not a visible one) and
# not DETACHED_PROCESS (no console at all, under which the pipe dies silently
# and the launcher stops at its first line).
_LAUNCHER_CMD = (
    "@echo off\r\n"
    "rem OpenMAD update launcher, written by the app. Safe to delete.\r\n"
    "rem %1 installer  %2 installer log  %3 PID to wait for  %4 max tries\r\n"
    "rem %5 app to restart afterwards, or \"\" not to\r\n"
    'echo [%DATE% %TIME%] waiting for PID %~3 > "%~dp0launcher.log"\r\n'
    "set TRIES=0\r\n"
    ":wait\r\n"
    'tasklist /FI "PID eq %~3" /NH 2>nul | find "%~3" >nul\r\n'
    "if errorlevel 1 goto run\r\n"
    "set /a TRIES+=1\r\n"
    "if %TRIES% GEQ %~4 goto run\r\n"
    "ping -n 2 127.0.0.1 >nul\r\n"
    "goto wait\r\n"
    ":run\r\n"
    'echo [%DATE% %TIME%] app gone after %TRIES%s, starting installer'
    ' >> "%~dp0launcher.log"\r\n'
    "%1 /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG=%2\r\n"
    'echo [%DATE% %TIME%] installer exit code %ERRORLEVEL%'
    ' >> "%~dp0launcher.log"\r\n'
    'if "%~5"=="" goto done\r\n'
    f"ping -n {_RESTART_SETTLE_S + 1} 127.0.0.1 >nul\r\n"
    'echo [%DATE% %TIME%] restarting %~5 >> "%~dp0launcher.log"\r\n'
    "start \"\" %5\r\n"
    'echo [%DATE% %TIME%] restart issued >> "%~dp0launcher.log"\r\n'
    ":done\r\n"
)


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


def _wait_for_file(path: Path, timeout_s: float) -> bool:
    """Whether path appears within timeout_s. Used to confirm a spawn took."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


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

        d = _download_dir()
        log = d / "install.log"
        launcher = d / _LAUNCHER_NAME
        launcher_log = d / "launcher.log"

        # Rewritten every time rather than only when missing, so a launcher
        # left behind by an older version is never the one that runs.
        launcher.write_text(_LAUNCHER_CMD, encoding="ascii", newline="")
        launcher_log.unlink(missing_ok=True)

        # See _LAUNCHER_CMD for why this is a script file taking arguments and
        # not a command string, and why CREATE_NO_WINDOW rather than
        # DETACHED_PROCESS. The launcher outlives this process on purpose.
        # What to start when the install finishes. sys.executable is this very
        # OpenMAD.exe, which the installer overwrites in place, so it is the
        # right path by construction rather than a guess at the install
        # directory. Empty from a source checkout, where there is nothing an
        # installer would have replaced.
        restart = sys.executable if getattr(sys, "frozen", False) else ""

        argv = [str(launcher), str(installer), str(log),
                str(os.getpid()), str(_LAUNCHER_MAX_TRIES), restart]
        try:
            subprocess.Popen(
                argv,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               | subprocess.CREATE_NEW_PROCESS_GROUP),
                close_fds=True,
            )
        except Exception as exc:
            self._set(state="error", error=f"could not start the installer: {exc}")
            raise

        # Never quit on the strength of a Popen that returned without error.
        #
        # Popen succeeding only means a process was created; it says nothing
        # about whether the launcher got as far as being able to run the
        # installer. The bug this replaced failed exactly there -- the process
        # started, cmd rejected the command line, and the app quit anyway, so
        # the user saw the app vanish with no update and nothing to read.
        #
        # Writing its log is the launcher's first action, so its appearance is
        # proof the script is executing. Without it, stay open and say so.
        if not _wait_for_file(launcher_log, _LAUNCH_CONFIRM_S):
            msg = ("the update launcher did not start, so the app has not been "
                   "closed. The downloaded installer can be run by hand: "
                   f"{installer}")
            self._set(state="error", error=msg)
            raise RuntimeError(msg)

        # Quitting is the caller's job, and it has to happen: the installer
        # cannot replace OpenMAD.exe while this process still has it open. The
        # launcher is watching this PID, so the sooner this returns the sooner
        # the install starts.
        if on_exit is not None:
            threading.Timer(1.0, on_exit).start()
        return {"installing": True, "log": str(log),
                "launcher_log": str(launcher_log),
                "installer": str(installer)}


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
