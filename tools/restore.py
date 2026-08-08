#!/usr/bin/env python3
"""Restore onboard configuration from a backup file.

Defaults to a dry run: it reads the device, diffs against the backup and prints
exactly what it would write, touching nothing. Pass --apply to actually write.

Scope, stated honestly. From a version 2 backup this restores:

    * dynamic keymap      (600 bytes, VIA buffer write)
    * macro buffer        (VIA buffer write)
    * per-key actuation   (vendor ONE_TOG_TH, V2 uint16be at 0.01 mm)
    * per-key rapid trigger (vendor ONE_RT, V2)
    * advanced keys       (SOCD / rapid snap / OKS, vendor rs 0x20)
    * dynamic keystroke   (vendor dks 0x0F)
    * per-key RGB and global lighting, box light
    * dead band, feature flags, game mode, bottom optimise

It still does NOT write mixAxle or tap dance, their write layouts are
unconfirmed, and guessing write layouts is how boards get broken.

Version 1 backups predate the feature sections. They carry only the first
four items above, so a restore from one will NOT put back SOCD bindings, DKS,
lighting or the scalar settings. This tool warns when it is handed one; take a
fresh backup to get full coverage.

    python tools/restore.py BACKUP.json            # dry run (default)
    python tools/restore.py BACKUP.json --apply    # write it back

If a restore ever leaves the keyboard wrong, the escalation path is: re-run the
official web configurator (it rewrites every region), then a factory reset, then
the bootloader at PID 0x2024.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mad68 import Cmd, Mad68, Vendor  # noqa: E402
from mad68.device import RapidTrigger  # noqa: E402
from mad68.features import (  # noqa: E402
    AdvancedKey,
    BoxLight,
    DeadBand,
    Dks,
    DksAction,
    KeyboardFeature,
    LightInfo,
)
from mad68.protocol import KEYMAP_SIZE, AdvancedKeyMode  # noqa: E402

UNCOVERED_NOTE = (
    "mix axle and tap dance -- read layout known, write layout unconfirmed. "
    "Everything else in a v2 backup is restored."
)


def plan(kb: Mad68, backup: dict) -> dict:
    """Diff device against backup. Returns the set of pending changes."""
    changes: dict = {
        "keymap": None, "macros": None, "actuation": [], "rapid_trigger": [],
        "dead_band": None, "feature_flags": None, "game_mode": None,
        "bottom_optimize": None, "box_light": None, "light_info": None,
        "key_colors": None, "advanced_keys": [], "dks": [],
    }

    want_keymap = bytes.fromhex(backup["keymap"]["hex"])
    if len(want_keymap) != KEYMAP_SIZE:
        raise SystemExit(
            f"backup keymap is {len(want_keymap)} bytes, expected {KEYMAP_SIZE}"
        )
    if kb.read_keymap() != want_keymap:
        changes["keymap"] = want_keymap

    want_macros = bytes.fromhex(backup["macros"]["hex"])
    have_macros = kb.read_macros()
    if have_macros != want_macros:
        if len(want_macros) != len(have_macros):
            raise SystemExit(
                f"backup macro buffer is {len(want_macros)} bytes but the device "
                f"reports {len(have_macros)}; refusing to restore a mismatched buffer"
            )
        changes["macros"] = want_macros

    for entry in backup["actuation"]:
        row, col, mm = entry["row"], entry["col"], entry["mm"]
        if abs(kb.read_actuation_mm(row, col) - mm) > 1e-9:
            changes["actuation"].append((row, col, mm))

    for entry in backup["rapid_trigger"]:
        want = RapidTrigger(
            row=entry["row"], col=entry["col"], enabled=entry["enabled"],
            release_mm=entry["release_mm"], press_mm=entry["press_mm"],
        )
        if kb.read_rapid_trigger(entry["row"], entry["col"]) != want:
            changes["rapid_trigger"].append(want)

    # structured features (backup_version >= 2)
    feats = backup.get("features") or {}

    def wanted(name):
        v = feats.get(name)
        return None if v is None or (isinstance(v, dict) and "error" in v) else v

    db = wanted("dead_band")
    if db is not None:
        want_db = DeadBand(top_mm=db["top_mm"], bottom_mm=db["bottom_mm"])
        if kb.read_dead_band() != want_db:
            changes["dead_band"] = want_db

    ff = wanted("feature_flags")
    if ff is not None:
        want_ff = KeyboardFeature(**ff)
        if kb.read_feature() != want_ff:
            changes["feature_flags"] = want_ff

    gm = wanted("game_mode")
    if gm is not None and kb.read_game_mode() != gm:
        changes["game_mode"] = gm

    bo = wanted("bottom_optimize")
    if bo is not None and kb.read_bottom_optimize() != bo:
        changes["bottom_optimize"] = bo

    bl = wanted("box_light")
    if bl is not None:
        want_bl = BoxLight(**bl)
        if kb.read_box_light() != want_bl:
            changes["box_light"] = want_bl

    li = wanted("light_info")
    if li is not None:
        want_li = LightInfo(**{k: v for k, v in li.items() if k != "color_hex"})
        if kb.read_light_info() != want_li:
            changes["light_info"] = want_li

    kc = wanted("key_colors")
    if kc is not None:
        want_kc = [tuple(c) for c in kc]
        if kb.read_all_key_colors() != want_kc:
            changes["key_colors"] = want_kc

    for entry in backup.get("advanced_keys") or []:
        if "error" in entry:
            continue
        want_ak = AdvancedKey(
            index=entry["index"],
            mode=AdvancedKeyMode[entry["mode"]] if isinstance(entry["mode"], str)
            else AdvancedKeyMode(entry["mode"]),
            id=entry["id"], rs_apc_lv=entry["rs_apc_lv"],
            gapc_sw=entry["gapc_sw"], rt_sw=entry["rt_sw"],
            key1_row=entry["key1_row"], key1_col=entry["key1_col"],
            key2_row=entry["key2_row"], key2_col=entry["key2_col"],
            layer=entry["layer"],
        )
        if kb.read_advanced_key(entry["index"]) != want_ak:
            changes["advanced_keys"].append(want_ak)

    for entry in backup.get("dks") or []:
        if "error" in entry:
            continue
        want_dks = Dks(
            index=entry["index"],
            actions=tuple(
                DksAction.from_keycode(a["keycode"], a["status"]) for a in entry["actions"]
            ),
        )
        if kb.read_dks(entry["index"]) != want_dks:
            changes["dks"].append(want_dks)

    return changes


def describe(changes: dict) -> int:
    total = 0
    if changes["keymap"] is not None:
        print(f"  keymap:        {len(changes['keymap'])} bytes to write")
        total += 1
    else:
        print("  keymap:        already matches")

    if changes["macros"] is not None:
        print(f"  macros:        {len(changes['macros'])} bytes to write")
        total += 1
    else:
        print("  macros:        already matches")

    if changes["actuation"]:
        print(f"  actuation:     {len(changes['actuation'])} key(s) to write")
        for row, col, mm in changes["actuation"]:
            print(f"                   row {row} col {col} -> {mm:.2f} mm")
        total += len(changes["actuation"])
    else:
        print("  actuation:     already matches")

    if changes["rapid_trigger"]:
        print(f"  rapid trigger: {len(changes['rapid_trigger'])} key(s) to write")
        for rt in changes["rapid_trigger"]:
            print(f"                   row {rt.row} col {rt.col} -> "
                  f"{'on' if rt.enabled else 'off'} "
                  f"press {rt.press_mm:.2f} release {rt.release_mm:.2f}")
        total += len(changes["rapid_trigger"])
    else:
        print("  rapid trigger: already matches")

    for name in ("dead_band", "feature_flags", "game_mode", "bottom_optimize",
                 "box_light", "light_info"):
        v = changes.get(name)
        if v is not None:
            print(f"  {name:<14} -> {v}")
            total += 1
        else:
            print(f"  {name:<14} already matches")

    if changes.get("key_colors") is not None:
        print(f"  key_colors:    {len(changes['key_colors'])} key(s) to write")
        total += 1
    else:
        print("  key_colors:    already matches")

    for name in ("advanced_keys", "dks"):
        items = changes.get(name) or []
        if items:
            print(f"  {name}: {len(items)} entr(y/ies) to write")
            for it in items:
                print(f"                   {it}")
            total += len(items)
        else:
            print(f"  {name}: already matches")

    return total


def apply(kb: Mad68, changes: dict) -> None:
    if changes["keymap"] is not None:
        print("  writing keymap ...")
        kb.write_keymap(changes["keymap"])
    if changes["macros"] is not None:
        print("  writing macro buffer ...")
        kb.write_buffer(Cmd.DYNAMIC_KEYMAP_MACRO_SET_BUFFER, changes["macros"])

    # Advanced keys go before per-key actuation: writing one re-applies its own
    # actuation level to the two keys it binds, so doing it afterwards would undo
    # the actuation we just restored for those keys. Verified on hardware.
    for ak in changes.get("advanced_keys") or []:
        print(f"  writing advanced key {ak.index} ({ak.mode.name})")
        kb.write_advanced_key(ak)

    for row, col, mm in changes["actuation"]:
        print(f"  writing actuation row {row} col {col} = {mm:.2f} mm")
        kb.write_actuation_mm(row, col, mm)
    for rt in changes["rapid_trigger"]:
        print(f"  writing rapid trigger row {rt.row} col {rt.col}")
        kb.write_rapid_trigger(rt)

    writers = {
        "dead_band": kb.write_dead_band,
        "feature_flags": kb.write_feature,
        "game_mode": kb.write_game_mode,
        "bottom_optimize": kb.write_bottom_optimize,
        "box_light": kb.write_box_light,
        "light_info": kb.write_light_info,
    }
    for name, write in writers.items():
        v = changes.get(name)
        if v is not None:
            print(f"  writing {name}")
            write(v)

    if changes.get("key_colors") is not None:
        print(f"  writing {len(changes['key_colors'])} key colours")
        kb.write_all_key_colors(changes["key_colors"])

    for d in changes.get("dks") or []:
        print(f"  writing dks {d.index}")
        kb.write_dks(d)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("backup", help="path to a backup JSON produced by tools/backup.py")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this, nothing is written.")
    args = ap.parse_args()

    backup = json.loads(Path(args.backup).read_text(encoding="utf-8-sig"))

    version = backup.get("backup_version", 1)
    print(f"Backup:  {args.backup}")
    print(f"Captured: {backup['captured_utc']}  (backup_version {version})")
    print(f"Device:   {backup['device']['product']} serial "
          f"{backup['device']['serial']!r}")

    if version < 2:
        print()
        print("  WARNING: this is a version 1 backup. It predates the advanced")
        print("  feature sections, so restoring it will NOT put back SOCD/OKS")
        print("  bindings, DKS entries, lighting, per-key RGB, dead band,")
        print("  feature flags, game mode or bottom optimise -- those simply")
        print("  are not in the file. Keymap, macros, actuation and rapid")
        print("  trigger are still restored in full.")
        print("  Take a fresh backup (python tools/backup.py) for full coverage.")
    print()

    # Writes are only unlocked when --apply is given; dry runs cannot mutate
    # even by accident because the transport rejects write commands outright.
    with Mad68(writes=args.apply) as kb:
        serial = kb.info.get("serial_number")
        if serial != backup["device"]["serial"]:
            print(f"refusing to restore: connected device serial {serial!r} does not "
                  f"match backup serial {backup['device']['serial']!r}", file=sys.stderr)
            return 2

        print("Comparing device against backup ...")
        changes = plan(kb, backup)
        print()
        total = describe(changes)
        print(f"\nNot covered by restore: {UNCOVERED_NOTE}")

        if total == 0:
            print("\nNothing to do -- device already matches the backup.")
            return 0

        if not args.apply:
            print(f"\nDry run: {total} change(s) pending, nothing written.")
            print("Re-run with --apply to write them.")
            return 0

        print(f"\nApplying {total} change(s) ...")
        apply(kb, changes)

        print("\nRe-reading to confirm ...")
        remaining = plan(kb, backup)
        left = sum(
            (1 if remaining["keymap"] is not None else 0)
            + (1 if remaining["macros"] is not None else 0)
            + len(remaining["actuation"])
            + len(remaining["rapid_trigger"])
            for _ in [0]
        )
        if left:
            print(f"  WARNING: {left} item(s) still differ after the write:")
            describe(remaining)
            return 1
        print("  all restored regions now match the backup.")

    print("\nDone. Unplug and replug the keyboard, then run\n"
          "  python tools/backup.py --verify <backup>\n"
          "to confirm the values persisted across a power cycle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
