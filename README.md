# OpenMAD

[![Logo](assets/logo.png)](https://sisyphe.acciaw.me)

[![Static Badge](https://img.shields.io/badge/License-GNU%20GPLv3-green)](https://creativecommons.org/licenses/by-sa/4.0/?ref=chooser-v1)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platforms](https://img.shields.io/badge/Platform-Windows-lightgrey)]()

A local driver for the **MAD68 HE** magnetic-switch keyboard, replacing the
web-only configurator at `hub.fgg.com.cn`.

It does everything the stock tool does, plus the things a web page can't: named
profiles stored as files, and automatic profile switching based on which
application is in the foreground.

---

## Install

Download `OpenMAD-Setup-x.y.z.exe` from the releases page and run it. It installs
per-user and never asks for admin rights (the keyboard is reached over raw HID,
which Windows exposes to ordinary user processes).

The app lives in the system tray. Right-click it for the quick menu, **Open** launches
the configurator in your browser.

## What it does

**Per-key settings:** actuation point, rapid trigger, dead zone, and the
advanced key modes: Dynamic Key Stroke, Hold/Click, Rappy Snappy, SOCD.

**Lighting:** all 21 effects the firmware supports, per-key colours, brightness
and speed, plus a colour-calibration panel (the LEDs are not colour-balanced
from the factory).

**Keymap and macros:** four layers, 16 macro slots with a recorder.

**Profiles:** stored as JSON in your data folder. One can be written to the
keyboard's onboard memory. The rest are applied by the driver on demand.

**App switching:** bind one or more executables to a profile and the driver
switches when that app takes focus. One profile can be marked the default, used
whenever nothing else matches.

---

## How this was reverse-engineered

The stock configurator is a Vue app protected with `javascript-obfuscator`. No
firmware was disassembled and no traffic was intercepted. Everything came from
reading the shipped JavaScript and confirming each finding against the hardware.

**1. Recover the sources.** The bundle uses the string-array transform: a
rotating array, an offset decoder, and per-scope aliases. The decoder does no
encryption (it is an indexed lookup) so once the array is rotated into its
correct order every call can be resolved statically and the literal inlined.
That is what `research/deobfuscate.js` does, after Prettier re-expands the
minified source.

**2. Find the right code.** The app code-splits per device family. The device
table maps USB VID/PID to a controller class. The MAD68 HE's (`0x373B:0x1058`, "MAD 68
RGB") binds to the `DuckBread` controller. The lighting UI turned out to live in a lazily-loaded chunk
that `index.html` never references, only a dynamic import does.

**3. Read the packet classes.** Each request is a small class with typed
accessors over a `DataView`, which gives exact byte offsets. Cross-referencing
the enums (`Mu` for VIA commands, `Bu` for vendor sub-commands, `Eu` for the
flash operation) reconstructs the wire format without guessing.

**4. Confirm against the hardware.** Every claim was checked by writing to the
keyboard and reading it back. This is what caught the things the source alone
would not tell you:

- brightness above 210 is silently dropped while the rest of the same packet
  still applies, so the write looks successful and changes nothing,
- `read_light_info` returns the colour *after* a gamma curve, so a read never
  matches the write,
- writing advanced keys re-applies actuation to the two keys they bind, so the
  write order in a profile matters.

---

## Not damaging the keyboard

This was the design constraint from the start, and it shapes the architecture.

**Automatic switching never touches flash.** The vendor's protocol has a
`flashOp` field. `NORMAL` applies values immediately without committing them while
`ERASE_AND_WRITE` persists. Profile application uses `NORMAL`, so switching
profiles on every alt-tab costs no write endurance. Only two actions ever
commit: "Save to keyboard" and "Save profile onboard".

**Key bindings are never applied automatically.** Actuation, rapid trigger and
lighting all ride the vendor channel and can be applied with `flashOp` set to
`NORMAL`, which does not touch persistent memory. Key bindings do not: they go
through VIA's dynamic-keymap command, which has no volatile mode, so every
write is an EEPROM write. Each profile stores and can edit its own key
bindings, but switching to it never writes them, since that would spend real
write endurance every time the foreground window changed. Applying them is a
deliberate action, either from Change Key Setting or the tray.

**Reads are free.** The live telemetry the gauge uses is read-only.

**Everything is reversible.** `tools/backup.py` snapshots the full onboard state
to JSON and `tools/restore.py` writes it back. Take a backup before your first
write.

**The dangerous commands are gated.** Calibration, factory reset, EEPROM reset
and bootloader jump are refused by the transport unless a caller explicitly opts
in, and the UI additionally requires a typed confirmation. Firmware flashing is
not implemented at all.

**Factory reset is a keyboard operation, not an OpenMAD one.** It wipes the
board's own onboard memory (calibration, whatever profile was saved to it) and
nothing else. It will not fix a crashed tray app, a stuck configurator, or a
corrupted profile file. Those live on your PC, not the keyboard, and a factory
reset cannot touch them.

---

## Building from source

Requires Python 3.11+ and Windows.

```bash
git clone <repo> && cd madlions_driver
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python tools/tray.py          # tray app (this is the product)
python tools/hud.py           # configurator only, no tray
```

### Building a release

```bash
pip install pyinstaller
pyinstaller packaging/mad68.spec        # -> dist/OpenMAD.exe
```

`dist/OpenMAD.exe` is standalone: no Python, no dependencies, no install. Enough
to hand someone a single file.

For the installer, install [Inno Setup](https://jrsoftware.org/isinfo.php) and
then:

```bash
iscc packaging/mad68.iss                # -> packaging/Output/OpenMAD-Setup-1.0.0.exe
```

The installer adds Start-menu and optional desktop shortcuts, and offers to
start the app at sign-in.

### Releasing

The version and repository live in one file, `version.ini` at the repository
root. The app reads it at runtime, the PyInstaller spec names the executable
from it, and the installer script reads it with the Inno preprocessor, so there
is nothing to keep in sync.

```ini
[app]
name = OpenMAD
version = 1.0.0
repo = Acciaw/OpenMAD
```

To cut a release: bump `version`, rebuild both artifacts, then tag the commit
with the same number prefixed by `v` and publish a GitHub release with the two
binaries attached.

```bash
git tag v1.0.0 && git push origin v1.0.0
gh release create v1.0.0 dist/OpenMAD.exe   packaging/Output/OpenMAD-Setup-1.0.0.exe --title "OpenMAD 1.0.0"
```

The update check reads `/repos/<repo>/releases/latest` and compares the tag
numerically against the running version. The release must be published rather
than a draft: the latest endpoint skips drafts and prereleases, so a draft looks
like no update at all.

---

## Repository layout

| | |
|---|---|
| `src/mad68/` | the driver: protocol, transport, profiles, switcher, web UI |
| `tools/` | what ships: `tray.py`, `hud.py`, and the `backup.py` / `restore.py` safety net |
| `dev/` | development only, gitignored — test harness and hardware probes |
| `packaging/` | PyInstaller spec and Inno Setup script |
| `assets/` | logo and icon (see `assets/README.md`) |
| `research/` | protocol notes and the deobfuscation tool (tracked); the
deobfuscated vendor bundle itself (gitignored) |
| `profiles/` | profile JSON |

`research/notes/protocol.md` and `research/deobfuscate.js` are original work and
stay tracked. The deobfuscated copy of the vendor's own JavaScript that
`protocol.md` was written from — `research/beautified/`, `research/clear/`,
`research/webapp/`, `research/strings/` — is gitignored: it is a reproduction
of someone else's copyrighted source, not needed to build or run anything, and
regenerable by running `research/deobfuscate.js` against a fresh download of
the vendor's bundle if you need to re-derive something.

### Development checks

```bash
python dev/smoke.py            # every entry point, with the keyboard connected
python dev/smoke.py --offline  # the subset that needs no hardware
python dev/check_pages.py      # parses the web UI and renders every tab
```

`check_pages.py` is worth knowing about: it executes the page against a DOM stub
and renders every tab, sub-tab, editor and lighting effect. Parsing alone cannot
catch a `ReferenceError`, and one shipped that way — a missing constant threw
while building the Advanced Key list, which meant no event handlers were ever
attached and every button in that section was dead.

---

## Compatibility

Written for and tested on the MAD68 HE, USB `0x373B:0x1058` ("MAD 68 RGB").

The `DuckBread` controller backs several other boards in the same family
(MAD60 HE, MAD68 HE V2, MAD63 HE and others). Much of this would likely work on
them, but the matrix size and LED count are hardcoded and nothing else has been
tested. Take a backup first.

Not affiliated with, endorsed by, or supported by the manufacturer.

This project's own code is original work, and the protocol notes describe
facts learned by reverse engineering (byte offsets, enum values, command IDs)
rather than reproducing anyone's source. The vendor's own JavaScript, even
deobfuscated, is their copyrighted work and is deliberately not distributed
here; see `research/` in the repository layout above for exactly what is and
is not tracked.

---

## Firmware updates

This driver does not flash firmware, on purpose. Flashing is the one operation
that can brick the board with no recovery path this project controls, and the
official configurator already does it. The Firmware button in the sidebar links
straight to `hub.fgg.com.cn` rather than attempting it locally.

The more interesting risk is the other direction: what happens here if you
flash *new* firmware through the official tool. Every packet layout in this
driver, every byte offset, every enum, was reverse engineered from one specific
firmware build and confirmed against it on real hardware. A firmware update
could change any of that. It is not hypothetical — the vendor's own app does
the same kind of version check internally, gating whether advanced keys are
even offered based on a firmware version number in its device table. Wire
formats do change between firmware builds.

So the driver checks. It reads the keyboard's own reported protocol version on
every connect and compares it against the version this build was verified
against (`KNOWN_PROTOCOL_VERSION` in `src/mad68/protocol.py`). If they match,
nothing is shown — this is the common case and should stay invisible. If they
do not, a banner appears across every tab saying so, naming the live version
and recommending a backup and the official configurator until it is confirmed
safe.

This is deliberately a warning, not a hard block. A version bump might change
nothing this driver touches, and refusing to work on principle would be worse
than the risk it is guarding against. But it means a firmware update is never
silently unsafe: either everything still lines up, or you are told plainly that
it might not, before anything gets written.

If you do hit a mismatch and want it supported, that is a real reverse
engineering task, not a settings change: recheck the packet layouts in
`research/notes/protocol.md` against the new firmware, confirm each change on
hardware the way the rest of this project's findings were confirmed, and update
`KNOWN_PROTOCOL_VERSION` only once that is done.
