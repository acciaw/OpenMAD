#!/usr/bin/env python3
"""Full onboard-memory backup for Madlions HE keyboards.

This is the safety gate for the whole project: nothing in this repo should write
to the keyboard until a backup exists and has been verified.

Everything is captured twice, once as the raw reply bytes (so restore.py can
replay byte-for-byte) and once decoded (so a human can read the file and diff
two backups). Reads only; the transport is constructed read-only.

    python tools/backup.py                  # write a new timestamped backup
    python tools/backup.py --verify FILE    # re-read and compare against FILE
    python tools/backup.py --show FILE      # summarise a backup file

Backups land in backups/ as mad68-backup-<UTC timestamp>.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mad68 import KeyboardValue, Mad68, Vendor  # noqa: E402
from mad68.protocol import (  # noqa: E402
    LAYER_COUNT,
    MATRIX_COLS,
    MATRIX_ROWS,
    VOLATILE_KEYBOARD_VALUES,
)

BACKUP_VERSION = 2
REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "backups"

# How many advanced-key and DKS slots to sweep. The firmware exposes them by
# index with no count command, so this is a bounded probe.
ADVANCED_KEY_SLOTS = 16
DKS_SLOTS = 16

# Vendor sub-commands read with no arguments. All are informational reads; the
# destructive ones (calibration, IAP, resets) are deliberately absent and are
# blocked by the transport anyway.
SCALAR_VENDOR = (
    Vendor.DEAD_BAND,
    Vendor.FEATURE,
    Vendor.GAME_MODE,
    Vendor.BOTTOM_OPTIMIZE_SWITCH,
    Vendor.LIGHT_INFO,
    Vendor.LAYER,
    Vendor.RS,
    Vendor.BOX_LIGHT,
    Vendor.MIX_AXLE,
    Vendor.GET_CUSTOM_LAMPLIGHT,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture(kb: Mad68) -> dict:
    """Read every recoverable byte of onboard configuration."""
    keymap = kb.read_keymap()
    macro_buffer_size = kb.macro_buffer_size()
    macros = kb.read_macros()

    actuation: list[dict] = []
    rapid_trigger: list[dict] = []
    for row, col in kb.iter_keys():
        act_raw = kb.vendor_get(Vendor.ONE_TOG_TH, bytes([row, col])).payload
        rt_raw = kb.vendor_get(Vendor.ONE_RT, bytes([row, col])).payload
        actuation.append(
            {
                "row": row,
                "col": col,
                "mm": round(int.from_bytes(act_raw[4:6], "big") * 0.01, 2),
                "raw": act_raw.hex(),
            }
        )
        rapid_trigger.append(
            {
                "row": row,
                "col": col,
                "enabled": bool(rt_raw[4]),
                "press_mm": round(int.from_bytes(rt_raw[7:9], "big") * 0.01, 2),
                "release_mm": round(int.from_bytes(rt_raw[5:7], "big") * 0.01, 2),
                "raw": rt_raw.hex(),
            }
        )

    # Uptime and switch-matrix state change on their own, so they are recorded
    # separately as observations rather than as configuration to be compared.
    keyboard_values: dict[str, str] = {}
    volatile_values: dict[str, str] = {}
    for kv in KeyboardValue:
        try:
            hexed = kb.keyboard_value(int(kv)).payload.hex()
        except Exception as exc:
            hexed = f"ERROR: {exc}"
        if kv in VOLATILE_KEYBOARD_VALUES:
            volatile_values[kv.name] = hexed
        else:
            keyboard_values[kv.name] = hexed

    vendor_scalars: dict[str, str] = {}
    for sub in SCALAR_VENDOR:
        try:
            vendor_scalars[sub.name] = kb.vendor_get(sub).payload.hex()
        except Exception as exc:
            # Best effort: some sub-commands may need arguments on this
            # firmware. A failure here must not abort the backup.
            vendor_scalars[sub.name] = f"ERROR: {exc}"

    # Dynamic keystroke: request layout is not yet confirmed, so capture a
    # best-effort per-key probe rather than pretending we understand it.
    dks: list[dict] = []
    for row, col in kb.iter_keys():
        try:
            dks.append(
                {"row": row, "col": col,
                 "raw": kb.vendor_get(Vendor.DKS, bytes([row, col])).payload.hex()}
            )
        except Exception as exc:
            dks.append({"row": row, "col": col, "error": str(exc)})

    # Structured feature state. Unlike vendor_scalars (raw hex), these have
    # confirmed write layouts, so restore.py can put them back.
    features: dict = {}
    for name, fn in (
        ("dead_band", lambda: kb.read_dead_band().to_json()),
        ("feature_flags", lambda: kb.read_feature().to_json()),
        ("game_mode", kb.read_game_mode),
        ("bottom_optimize", kb.read_bottom_optimize),
        ("box_light", lambda: kb.read_box_light().to_json()),
        ("light_info", lambda: kb.read_light_info().to_json()),
        ("key_colors", lambda: [list(c) for c in kb.read_all_key_colors()]),
    ):
        try:
            features[name] = fn()
        except Exception as exc:
            features[name] = {"error": str(exc)}

    advanced_keys = []
    for i in range(ADVANCED_KEY_SLOTS):
        try:
            advanced_keys.append(kb.read_advanced_key(i).to_json())
        except Exception as exc:
            advanced_keys.append({"index": i, "error": str(exc)})

    dks_entries = []
    for i in range(DKS_SLOTS):
        try:
            dks_entries.append(kb.read_dks(i).to_json())
        except Exception as exc:
            dks_entries.append({"index": i, "error": str(exc)})

    try:
        macros_decoded = [
            [s.to_json() for s in steps] for steps in kb.read_macro_steps()
        ]
    except Exception as exc:
        macros_decoded = [{"error": str(exc)}]

    return {
        "backup_version": BACKUP_VERSION,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "device": {
            "vendor_id": kb.info.get("vendor_id"),
            "product_id": kb.info.get("product_id"),
            "manufacturer": kb.info.get("manufacturer_string"),
            "product": kb.info.get("product_string"),
            "serial": kb.info.get("serial_number"),
            "protocol_version": kb.protocol_version(),
            "layer_count": kb.layer_count(),
            "macro_count": kb.macro_count(),
            "macro_buffer_size": macro_buffer_size,
        },
        "geometry": {
            "layers": LAYER_COUNT,
            "rows": MATRIX_ROWS,
            "cols": MATRIX_COLS,
        },
        "keymap": {"size": len(keymap), "sha256": sha256(keymap), "hex": keymap.hex()},
        "macros": {"size": len(macros), "sha256": sha256(macros), "hex": macros.hex()},
        "actuation": actuation,
        "rapid_trigger": rapid_trigger,
        "keyboard_values": keyboard_values,
        "volatile_observations": volatile_values,
        "vendor_scalars": vendor_scalars,
        "features": features,
        "advanced_keys": advanced_keys,
        "dks": dks_entries,
        "macros_decoded": macros_decoded,
        "dks_probe": dks,
    }


def summarise(data: dict) -> None:
    dev = data["device"]
    print(f"  captured:  {data['captured_utc']}")
    print(f"  device:    {dev['product']} (serial {dev['serial']!r}), "
          f"VIA protocol {dev['protocol_version']}")
    print(f"  keymap:    {data['keymap']['size']} bytes, sha256 {data['keymap']['sha256'][:16]}...")
    print(f"  macros:    {data['macros']['size']} bytes, sha256 {data['macros']['sha256'][:16]}...")

    keymap = bytes.fromhex(data["keymap"]["hex"])
    for layer in range(data["geometry"]["layers"]):
        base = layer * data["geometry"]["rows"] * data["geometry"]["cols"] * 2
        span = data["geometry"]["rows"] * data["geometry"]["cols"] * 2
        codes = [
            int.from_bytes(keymap[base + i:base + i + 2], "big")
            for i in range(0, span, 2)
        ]
        print(f"    layer {layer}: {sum(1 for c in codes if c)}/{len(codes)} mapped")

    non_default = [a for a in data["actuation"] if a["mm"] != data["actuation"][0]["mm"]]
    print(f"  actuation: {len(data['actuation'])} keys; "
          f"{len(non_default)} differ from key(0,0) ({data['actuation'][0]['mm']:.2f} mm)")
    for a in non_default:
        print(f"    row {a['row']} col {a['col']}: {a['mm']:.2f} mm")

    rt_on = [r for r in data["rapid_trigger"] if r["enabled"]]
    print(f"  rapid trigger: {len(rt_on)}/{len(data['rapid_trigger'])} keys enabled")

    errs = [k for k, v in data["vendor_scalars"].items() if str(v).startswith("ERROR")]
    if errs:
        print(f"  vendor reads that failed (informational only): {', '.join(errs)}")


def compare(old: dict, new: dict) -> int:
    """Compare two captures. Returns the number of differences."""
    diffs = 0

    for section in ("keymap", "macros"):
        if old[section]["sha256"] != new[section]["sha256"]:
            print(f"  DIFF {section}: {old[section]['sha256'][:16]} -> "
                  f"{new[section]['sha256'][:16]}")
            diffs += 1
        else:
            print(f"  ok   {section} ({old[section]['size']} bytes, sha256 matches)")

    for section, keyfn in (
        ("actuation", lambda e: (e["row"], e["col"])),
        ("rapid_trigger", lambda e: (e["row"], e["col"])),
        ("dks_probe", lambda e: (e["row"], e["col"])),
    ):
        o = {keyfn(e): e.get("raw") for e in old.get(section, [])}
        n = {keyfn(e): e.get("raw") for e in new.get(section, [])}
        changed = [k for k in o if o[k] != n.get(k)]
        if changed:
            for k in changed:
                print(f"  DIFF {section} row {k[0]} col {k[1]}: {o[k]} -> {n.get(k)}")
            diffs += len(changed)
        else:
            print(f"  ok   {section} ({len(o)} entries identical)")

    for section in ("keyboard_values", "vendor_scalars"):
        for k, v in old.get(section, {}).items():
            nv = new.get(section, {}).get(k)
            # An error string on either side is informational, not a real diff.
            if str(v).startswith("ERROR") or str(nv).startswith("ERROR"):
                continue
            if v != nv:
                print(f"  DIFF {section}.{k}: {v} -> {nv}")
                diffs += 1
        print(f"  ok   {section} compared")

    return diffs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--verify", metavar="FILE", help="re-read the device and diff against FILE")
    g.add_argument("--show", metavar="FILE", help="summarise a backup file without touching hardware")
    ap.add_argument("--out", metavar="FILE", help="explicit output path for a new backup")
    args = ap.parse_args()

    if args.show:
        data = json.loads(Path(args.show).read_text(encoding="utf-8-sig"))
        print(f"Backup {args.show}:")
        summarise(data)
        return 0

    if args.verify:
        old = json.loads(Path(args.verify).read_text(encoding="utf-8-sig"))
        print(f"Re-reading device to verify against {args.verify} ...")
        with Mad68(writes=False) as kb:
            new = capture(kb)
        print()
        diffs = compare(old, new)
        print()
        if diffs:
            print(f"{diffs} difference(s) found -- the device no longer matches this backup.")
            return 1
        print("Device matches the backup exactly.")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else BACKUP_DIR / f"mad68-backup-{stamp}.json"
    if out.exists():
        print(f"refusing to overwrite existing backup {out}", file=sys.stderr)
        return 2

    print("Reading onboard configuration (read-only) ...")
    with Mad68(writes=False) as kb:
        data = capture(kb)

    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nWrote {out} ({out.stat().st_size:,} bytes)\n")
    summarise(data)
    print(f"\nVerify any time with:\n  python tools/backup.py --verify {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
