"""Named, file-based configuration profiles for Madlions HE keyboards.

A profile is a declarative description of *what the keyboard should be*. Applying
one diffs it against the live device and writes only the fields that actually
differ.

That minimal-diff behaviour is not just an optimisation. Vendor writes land in
the keyboard's non-volatile memory, which has a finite erase/write endurance. A
naive "write all 75 keys on every app switch" design would burn through that,
so apply() writes only genuine changes and reports exactly how many writes it
issued.

Profiles store actuation and rapid trigger as a default plus sparse
overrides, which is both far more readable than 75 rows and much cheaper to
diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .device import Mad68, RapidTrigger
from .features import (
    AdvancedKey,
    DeadBand,
    Dks,
    DksAction,
    KeyboardFeature,
    LightInfo,
    TapDance,
)
from .protocol import (
    ANIMATED_EFFECTS,
    COLOUR_EFFECTS,
    DEFAULT_EFFECT_COLOUR,
    DEFAULT_EFFECT_SPEED,
    KEYCODE_SIZE,
    MATRIX_COLS,
    MATRIX_ROWS,
    MAX_ACTUATION_WRITE,
    MAX_BUFFER_CHUNK,
    MAX_RT_WRITE,
    TOTAL_KEYS,
    AdvancedKeyMode,
    FlashOp,
)

# Advanced-key slots a profile snapshots. The firmware exposes them by index
# with no count command, so this is a bounded sweep.
ADVANCED_KEY_SLOTS = 16

# Lighting effect index that hands control of every LED to the per-key colour
# table. Confirmed on hardware: with any other effect selected the animation
# owns the LEDs, per-key colours have no visible result, and reading them back
# returns the live rendered frame rather than what was written.
PER_KEY_EFFECT = 1

# Per-channel slack when comparing key colours. The LEDs quantise PWM, so a
# written 40 reads back as 36; without slack the diff never converges.
COLOR_TOLERANCE = 8

# v2 adds lighting, per-key colours and advanced keys, so a profile can express
# a complete keyboard state ("1.50 mm everywhere, no LEDs, no SOCD") rather than
# only actuation and rapid trigger. v1 files still load; their extra sections
# are simply absent and left untouched on apply.
PROFILE_VERSION = 2


def _key(row: int, col: int) -> str:
    """Stable string key for JSON objects: 'r,c'."""
    return f"{row},{col}"


def _parse_key(text: str) -> tuple[int, int]:
    row, col = text.split(",")
    return int(row), int(col)


@dataclass(frozen=True)
class RapidTriggerSpec:
    """Rapid-trigger settings without the row/col binding."""

    enabled: bool
    press_mm: float
    release_mm: float

    def as_tuple(self) -> tuple[bool, float, float]:
        """Value identity, for grouping and choosing a profile default."""
        return (self.enabled, self.press_mm, self.release_mm)

    def to_json(self) -> dict:
        return {
            "enabled": self.enabled,
            "press_mm": self.press_mm,
            "release_mm": self.release_mm,
        }

    @classmethod
    def from_json(cls, d: dict) -> "RapidTriggerSpec":
        return cls(
            enabled=bool(d["enabled"]),
            press_mm=float(d["press_mm"]),
            release_mm=float(d["release_mm"]),
        )

    @classmethod
    def from_device(cls, rt: RapidTrigger) -> "RapidTriggerSpec":
        return cls(enabled=rt.enabled, press_mm=rt.press_mm, release_mm=rt.release_mm)


@dataclass
class Profile:
    """A named keyboard configuration.

    actuation_default / rapid_trigger_default apply to every key; the
    _overrides maps carry the exceptions, keyed 'row,col'.

    keymap is optional and stored as hex. It is omitted by default because
    swapping the whole keymap on every app change is a much heavier operation
    than adjusting actuation, and most per-app profiles only want the latter.
    """

    name: str
    actuation_default: float
    rapid_trigger_default: RapidTriggerSpec
    actuation_overrides: dict[str, float] = field(default_factory=dict)
    rapid_trigger_overrides: dict[str, RapidTriggerSpec] = field(default_factory=dict)
    keymap_hex: str | None = None
    description: str = ""

    # Full-config sections. None means "this profile does not manage that",
    # so applying it leaves the current setting alone, which is what keeps v1
    # profiles working and lets a profile deliberately own only part of the
    # keyboard.
    key_colors: list[list[int]] | None = None
    light: dict | None = None
    advanced_keys: list[dict] | None = None
    # Device-wide switches the profile drives on apply: feature flags, bottom
    # optimise, game mode, dead band. The firmware keeps one set of these, but a
    # profile owning them is what makes "this profile has WASD swapped" work.
    performance: dict | None = None
    # Hold/Click (MT) bindings and Dynamic Key Stroke entries.
    tap_dance: list[dict] | None = None
    dks: list[dict] | None = None
    # Applications that activate this profile, mirrored into switcher.json as
    # one rule each. A list, because several games can share a profile.
    # Entries are {"exe": str, "title": str}; either may be empty, and both set
    # means both must match.
    triggers: list[dict] | None = None
    # Superseded by triggers. Still read so profiles written before the list
    # existed keep working, and still written out when a profile has exactly
    # one trigger so downgrading does not silently lose the binding.
    trigger_exe: str | None = None
    trigger_title: str | None = None

    # geometry helpers

    @staticmethod
    def all_keys() -> Iterable[tuple[int, int]]:
        for row in range(MATRIX_ROWS):
            for col in range(MATRIX_COLS):
                yield row, col

    def actuation_for(self, row: int, col: int) -> float:
        return self.actuation_overrides.get(_key(row, col), self.actuation_default)

    def rapid_trigger_for(self, row: int, col: int) -> RapidTriggerSpec:
        return self.rapid_trigger_overrides.get(_key(row, col), self.rapid_trigger_default)

    # serialisation

    def to_json(self) -> dict:
        out: dict = {
            "profile_version": PROFILE_VERSION,
            "name": self.name,
            "description": self.description,
            "actuation": {
                "default_mm": self.actuation_default,
                "overrides_mm": dict(sorted(self.actuation_overrides.items())),
            },
            "rapid_trigger": {
                "default": self.rapid_trigger_default.to_json(),
                "overrides": {
                    k: v.to_json() for k, v in sorted(self.rapid_trigger_overrides.items())
                },
            },
        }
        if self.keymap_hex is not None:
            out["keymap_hex"] = self.keymap_hex
        if self.key_colors is not None:
            out["key_colors"] = self.key_colors
        if self.light is not None:
            out["light"] = self.light
        if self.advanced_keys is not None:
            out["advanced_keys"] = self.advanced_keys
        if self.performance is not None:
            out["performance"] = self.performance
        if self.tap_dance is not None:
            out["tap_dance"] = self.tap_dance
        if self.dks is not None:
            out["dks"] = self.dks
        triggers = self.trigger_list()
        if triggers:
            out["triggers"] = triggers
        # Mirror a lone trigger into the old fields so a profile written here
        # still means something to anything reading the previous schema.
        if len(triggers) == 1:
            out["trigger_exe"] = triggers[0].get("exe", "")
            out["trigger_title"] = triggers[0].get("title", "")
        return out

    def trigger_list(self) -> list[dict]:
        """Triggers as a list, whichever schema the profile was written in."""
        if self.triggers is not None:
            out = []
            for t in self.triggers:
                exe = (t.get("exe") or "").strip()
                title = (t.get("title") or "").strip()
                if exe or title:
                    out.append({"exe": exe, "title": title})
            return out
        exe = (self.trigger_exe or "").strip()
        title = (self.trigger_title or "").strip()
        return [{"exe": exe, "title": title}] if (exe or title) else []

    @classmethod
    def from_json(cls, d: dict) -> "Profile":
        version = d.get("profile_version")
        if version not in (1, PROFILE_VERSION):
            raise ValueError(
                f"unsupported profile_version {version!r}, expected 1 or {PROFILE_VERSION}"
            )
        act = d["actuation"]
        rt = d["rapid_trigger"]
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            actuation_default=float(act["default_mm"]),
            actuation_overrides={k: float(v) for k, v in act.get("overrides_mm", {}).items()},
            rapid_trigger_default=RapidTriggerSpec.from_json(rt["default"]),
            rapid_trigger_overrides={
                k: RapidTriggerSpec.from_json(v) for k, v in rt.get("overrides", {}).items()
            },
            keymap_hex=d.get("keymap_hex"),
            key_colors=d.get("key_colors"),
            light=d.get("light"),
            advanced_keys=d.get("advanced_keys"),
            performance=d.get("performance"),
            tap_dance=d.get("tap_dance"),
            dks=d.get("dks"),
            triggers=d.get("triggers"),
            trigger_exe=d.get("trigger_exe"),
            trigger_title=d.get("trigger_title"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Profile":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    # capture

    @classmethod
    def from_device(cls, kb: Mad68, name: str, *, include_keymap: bool = False,
                    include_lighting: bool = True, include_advanced: bool = True,
                    description: str = "") -> "Profile":
        """Snapshot the live device, choosing the most common value as default."""
        keys = list(cls.all_keys())
        actuation = dict(zip(keys, kb.read_actuation_bulk()))
        triggers = {
            (rt.row, rt.col): RapidTriggerSpec.from_device(rt)
            for rt in kb.read_rapid_trigger_bulk()
        }

        act_default = _most_common(actuation.values())
        rt_default = _most_common(triggers.values())

        key_colors = light = advanced = None
        if include_lighting:
            try:
                key_colors = [list(c) for c in kb.read_all_key_colors()]
                li = kb.read_light_info()
                light = {"effect": li.effect, "speed": li.speed, "r": li.r,
                         "g": li.g, "b": li.b, "brightness": li.brightness}
            except Exception:
                key_colors = light = None
        if include_advanced:
            try:
                advanced = [kb.read_advanced_key(i).to_json()
                            for i in range(ADVANCED_KEY_SLOTS)]
            except Exception:
                advanced = None

        return cls(
            key_colors=key_colors,
            light=light,
            advanced_keys=advanced,
            name=name,
            description=description,
            actuation_default=act_default,
            actuation_overrides={
                _key(r, c): v for (r, c), v in actuation.items() if v != act_default
            },
            rapid_trigger_default=rt_default,
            rapid_trigger_overrides={
                _key(r, c): t for (r, c), t in triggers.items() if t != rt_default
            },
            keymap_hex=kb.read_keymap().hex() if include_keymap else None,
        )

    # apply

    # target state

    def target_actuation(self) -> list[float]:
        """Desired actuation in mm for all 75 keys, in linear key order."""
        return [self.actuation_for(r, c) for r, c in self.all_keys()]

    def target_rapid_trigger(self) -> list[RapidTrigger]:
        """Desired rapid trigger for all 75 keys, in linear key order."""
        out = []
        for row, col in self.all_keys():
            spec = self.rapid_trigger_for(row, col)
            out.append(
                RapidTrigger(row=row, col=col, enabled=spec.enabled,
                             release_mm=spec.release_mm, press_mm=spec.press_mm)
            )
        return out

    def keymap_status(self, kb: Mad68) -> dict:
        """Whether this profile's keymap matches what is on the keyboard.

        The keymap is never applied automatically, so a profile can be active
        while the board still holds a different one. This is what the UI uses
        to say so.
        """
        if self.keymap_hex is None:
            return {"has_keymap": False, "matches": True, "keys_differ": 0}
        want = bytes.fromhex(self.keymap_hex)
        have = kb.read_keymap()
        if have == want:
            return {"has_keymap": True, "matches": True, "keys_differ": 0}
        # Count differing keycodes rather than bytes, so the UI can say
        # "3 keys differ" instead of a meaningless byte count.
        differ = sum(
            1 for i in range(0, min(len(have), len(want)), KEYCODE_SIZE)
            if have[i:i + KEYCODE_SIZE] != want[i:i + KEYCODE_SIZE]
        )
        return {"has_keymap": True, "matches": False, "keys_differ": differ}

    def apply_keymap(self, kb: Mad68) -> int:
        """Write this profile's keymap to the keyboard. Deliberate action only.

        This is an EEPROM write, the one thing profile switching deliberately
        avoids, so it never happens on its own.
        """
        if self.keymap_hex is None:
            return 0
        want = bytes.fromhex(self.keymap_hex)
        if kb.read_keymap() == want:
            return 0
        kb.write_keymap(want)
        return -(-len(want) // MAX_BUFFER_CHUNK)

    def plan(self, kb: Mad68, *, include_keymap: bool = False) -> "ApplyPlan":
        """Diff this profile against the live device using bulk reads.

        Whole-array comparison rather than per-key: a bulk write of all 75 keys
        costs 26 packets and ~12 ms, so there is nothing to gain from writing a
        subset, and with FlashOp.NORMAL it costs no write endurance either.

        The keymap is the exception and is left out unless asked for. Actuation,
        rapid trigger and lighting all ride the vendor channel and can be
        applied with FlashOp.NORMAL, which does not touch persistent memory. The
        keymap goes through VIA's dynamic keymap command, which has no volatile
        mode at all: every write is an EEPROM write. Applying a keymap on every
        automatic profile switch would therefore spend real write endurance
        each time the foreground window changes.

        Defaulting to False means the automatic switcher cannot do that even by
        omission. Only a deliberate user action passes True.
        """
        have_act = kb.read_actuation_bulk()
        have_rt = kb.read_rapid_trigger_bulk()
        want_act = self.target_actuation()
        want_rt = self.target_rapid_trigger()

        act_changes = sum(
            1 for a, b in zip(have_act, want_act) if abs(a - b) > 1e-9
        )
        rt_changes = sum(1 for a, b in zip(have_rt, want_rt) if a != b)

        keymap = None
        if include_keymap and self.keymap_hex is not None:
            want_keymap = bytes.fromhex(self.keymap_hex)
            if kb.read_keymap() != want_keymap:
                keymap = want_keymap

        key_colors = None
        if self.key_colors is not None:
            want_kc = [tuple(c) for c in self.key_colors]
            # Reading per-key colour returns the LIVE rendered frame, not the
            # stored setpoint. While an animation effect runs those values change
            # constantly, so comparing them would report a difference on every
            # single poll and rewrite 9 packets forever. Only diff them when the
            # profile actually uses the per-key mode; otherwise write once.
            effect = (self.light or {}).get("effect")
            if effect == PER_KEY_EFFECT:
                try:
                    have = kb.read_all_key_colors()
                    live = kb.populated_mask()
                    # Two reasons a naive comparison never converges: the LEDs
                    # quantise PWM (a written 40 reads back as 36), and the 7
                    # unpopulated matrix positions have no LED at all so they
                    # always read black. Allow slack, and skip the dead slots.
                    if len(have) != len(want_kc) or any(
                        abs(a - b) > COLOR_TOLERANCE
                        for i, (hc, wc) in enumerate(zip(have, want_kc))
                        if i < len(live) and live[i]
                        for a, b in zip(hc, wc)
                    ):
                        key_colors = want_kc
                except Exception:
                    key_colors = want_kc

        light = None
        if self.light is not None:
            fields = {k: int(v) for k, v in self.light.items()
                      if k in ("effect", "speed", "r", "g", "b", "brightness")}
            # Profiles saved before the HUD had a speed control all carry 0,
            # which the firmware takes literally and renders as a frozen first
            # frame. Every animated effect in such a profile looks like it never
            # applied. Give them a usable speed rather than replay the bug.
            # (Fixed up here rather than after construction, LightInfo is a
            # frozen dataclass and will not take the assignment.)
            if (fields.get("effect") in ANIMATED_EFFECTS
                    and not 1 <= fields.get("speed", 0) <= 255):
                fields["speed"] = DEFAULT_EFFECT_SPEED
            # Same shape of bug in the colour: a profile that has never had one
            # picked carries r=g=b=0, and the firmware renders black as one dead
            # hue rather than the effect. Applying such a profile from the tray
            # is how a rainbow came up as a single colour without the HUD ever
            # being opened.
            if (fields.get("effect") in COLOUR_EFFECTS
                    and not any((fields.get("r", 0), fields.get("g", 0),
                                 fields.get("b", 0)))):
                (fields["r"], fields["g"], fields["b"]) = DEFAULT_EFFECT_COLOUR
            want_li = LightInfo(**fields)
            try:
                have_li = kb.read_light_info()
                # Only effect, speed and brightness read back as what was set.
                # The RGB fields return the live rendered colour, which changes
                # continuously under an animation, comparing them would report
                # a difference forever.
                if (have_li.effect, have_li.speed, have_li.brightness) != (
                        want_li.effect, want_li.speed, want_li.brightness):
                    light = want_li
            except Exception:
                light = want_li

        performance = None
        if self.performance is not None:
            try:
                cur = {
                    "wasd_switch": kb.read_feature().wasd_switch,
                    "mac_switch": kb.read_feature().mac_switch,
                    "win_lock": kb.read_feature().win_lock,
                    "nkro_switch": kb.read_feature().nkro_switch,
                    "rgb_area": kb.read_feature().rgb_area,
                    "bottom_optimize": kb.read_bottom_optimize(),
                    "game_mode": kb.read_game_mode(),
                }
                want = {k: int(v) for k, v in self.performance.items() if k in cur}
                if any(cur.get(k) != v for k, v in want.items()):
                    performance = self.performance
            except Exception:
                performance = self.performance

        advanced: list[AdvancedKey] = []
        if self.advanced_keys is not None:
            for entry in self.advanced_keys:
                if "error" in entry:
                    continue
                mode = entry["mode"]
                want_ak = AdvancedKey(
                    index=entry["index"],
                    mode=AdvancedKeyMode[mode] if isinstance(mode, str)
                    else AdvancedKeyMode(mode),
                    id=entry.get("id", 0), rs_apc_lv=entry.get("rs_apc_lv", 0),
                    gapc_sw=entry.get("gapc_sw", 0), rt_sw=entry.get("rt_sw", 0),
                    key1_row=entry.get("key1_row", 0), key1_col=entry.get("key1_col", 0),
                    key2_row=entry.get("key2_row", 0), key2_col=entry.get("key2_col", 0),
                    layer=entry.get("layer", 0))
                try:
                    if kb.read_advanced_key(entry["index"]) != want_ak:
                        advanced.append(want_ak)
                except Exception:
                    advanced.append(want_ak)

        tap_dance: list[TapDance] = []
        for entry in self.tap_dance or []:
            want_td = TapDance(index=int(entry["index"]), tap=int(entry.get("tap", 0)),
                               hold=int(entry.get("hold", 0)),
                               timer_ms=int(entry.get("timer_ms", 200)))
            try:
                if kb.read_tap_dance(want_td.index) != want_td:
                    tap_dance.append(want_td)
            except Exception:
                tap_dance.append(want_td)

        dks_writes: list[Dks] = []
        for entry in self.dks or []:
            want_dks = Dks(index=int(entry["index"]), actions=tuple(
                DksAction.from_keycode(a.get("keycode", 0), a.get("status", 0))
                for a in entry.get("actions", [])))
            try:
                if kb.read_dks(want_dks.index) != want_dks:
                    dks_writes.append(want_dks)
            except Exception:
                dks_writes.append(want_dks)

        return ApplyPlan(
            profile=self,
            tap_dance=tap_dance,
            dks=dks_writes,
            actuation=want_act if act_changes else None,
            rapid_trigger=want_rt if rt_changes else None,
            keymap=keymap,
            actuation_changes=act_changes,
            rapid_trigger_changes=rt_changes,
            key_colors=key_colors,
            light=light,
            advanced_keys=advanced,
            performance_patch=performance,
        )


@dataclass
class ApplyPlan:
    """What must be written to bring the device to a profile.

    actuation / rapid_trigger hold the full 75-key target arrays when a bulk
    write is needed, or None when the device already matches.
    """

    profile: Profile
    actuation: list[float] | None
    rapid_trigger: list[RapidTrigger] | None
    keymap: bytes | None
    actuation_changes: int = 0
    rapid_trigger_changes: int = 0
    key_colors: list[tuple[int, int, int]] | None = None
    light: LightInfo | None = None
    advanced_keys: list[AdvancedKey] = field(default_factory=list)
    performance_patch: dict | None = None
    tap_dance: list[TapDance] = field(default_factory=list)
    dks: list[Dks] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            self.actuation is None
            and self.rapid_trigger is None
            and self.keymap is None
            and self.key_colors is None
            and self.light is None
            and not self.advanced_keys
            and self.performance_patch is None
            and not self.tap_dance
            and not self.dks
        )

    @property
    def packet_estimate(self) -> int:
        n = 0
        if self.actuation is not None:
            n += -(-len(self.actuation) // MAX_ACTUATION_WRITE)
        if self.rapid_trigger is not None:
            n += -(-len(self.rapid_trigger) // MAX_RT_WRITE)
        if self.keymap is not None:
            n += -(-len(self.keymap) // 28)
        return n

    def describe(self) -> str:
        if self.is_empty:
            return f"'{self.profile.name}': already applied, nothing to write"
        lines = [
            f"'{self.profile.name}': {self.packet_estimate} packet(s) to write"
        ]
        if self.actuation is not None:
            lines.append(
                f"  actuation      {self.actuation_changes} key(s) differ "
                f"-> bulk write all {len(self.actuation)}"
            )
        if self.rapid_trigger is not None:
            lines.append(
                f"  rapid trigger  {self.rapid_trigger_changes} key(s) differ "
                f"-> bulk write all {len(self.rapid_trigger)}"
            )
        if self.keymap is not None:
            lines.append(f"  keymap         {len(self.keymap)} bytes")
        if self.key_colors is not None:
            lines.append(f"  key colours    {len(self.key_colors)} keys")
        if self.light is not None:
            lines.append(f"  lighting       effect {self.light.effect} "
                         f"{self.light.color_hex} bright {self.light.brightness}")
        if self.advanced_keys:
            for ak in self.advanced_keys:
                lines.append(f"  advanced key   slot {ak.index} -> {ak.mode.name}")
        return "\n".join(lines)

    def execute(self, kb: Mad68, *, persist: bool = False) -> int:
        """Apply the plan. Returns the number of packets sent.

        By default writes are volatile (FlashOp.NORMAL): they take effect
        immediately but do not consume flash write endurance, which is what makes
        automatic per-application switching sustainable. Pass persist=True only
        for a deliberate user-initiated save.
        """
        flash_op = FlashOp.ERASE_AND_WRITE if persist else FlashOp.NORMAL
        packets = 0

        # Order matters. Writing an advanced key re-applies its own actuation
        # level (rs_apc_lv) to the two keys it binds, so it must go BEFORE the
        # per-key actuation write, otherwise binding SOCD to A and D silently
        # resets those keys' actuation to whatever is in flash. Verified on
        # hardware: actuation set to 1.60 everywhere, then an advanced-key write
        # dropped exactly its two bound keys back to 0.30.
        for ak in self.advanced_keys:
            kb.write_advanced_key(ak)
            packets += 1
        for td in self.tap_dance:
            kb.write_tap_dance(td)
            packets += 1
        for d in self.dks:
            kb.write_dks(d)
            packets += 1
        if self.performance_patch is not None:
            p = self.performance_patch
            cur = kb.read_feature()
            kb.write_feature(KeyboardFeature(
                rgb_area=int(p.get("rgb_area", cur.rgb_area)),
                wasd_switch=int(p.get("wasd_switch", cur.wasd_switch)),
                mac_switch=int(p.get("mac_switch", cur.mac_switch)),
                win_lock=int(p.get("win_lock", cur.win_lock)),
                nkro_switch=int(p.get("nkro_switch", cur.nkro_switch))))
            if "bottom_optimize" in p:
                kb.write_bottom_optimize(int(p["bottom_optimize"]))
            if "game_mode" in p:
                kb.write_game_mode(int(p["game_mode"]))
            if "dead_band_top" in p or "dead_band_bottom" in p:
                kb.write_dead_band(DeadBand(
                    top_mm=float(p.get("dead_band_top", 0.0)),
                    bottom_mm=float(p.get("dead_band_bottom", 0.0))))
            packets += 4

        if self.actuation is not None:
            packets += kb.write_actuation_bulk(self.actuation, flash_op=flash_op)
        if self.rapid_trigger is not None:
            packets += kb.write_rapid_trigger_bulk(self.rapid_trigger, flash_op=flash_op)
        if self.keymap is not None:
            # The keymap is a VIA dynamic-keymap buffer and always persists;
            # there is no volatile variant for it.
            kb.write_keymap(self.keymap)
            packets += -(-len(self.keymap) // 28)
        if self.key_colors is not None:
            packets += kb.write_all_key_colors(list(self.key_colors))
        if self.light is not None:
            kb.write_light_info(self.light)
            packets += 1
        return packets


# The profile the driver creates on first run: a deliberately plain baseline --
# 1.50 mm everywhere, lighting off, rapid trigger off, no advanced-key bindings.
# Everything else is built by tuning the board and capturing it.
DEFAULT_PROFILE_NAME = "Default"


def default_profile(name: str = DEFAULT_PROFILE_NAME) -> Profile:
    return Profile(
        name=name,
        description="Plain baseline: 1.50 mm actuation, no lighting, "
                    "no rapid trigger, no advanced keys.",
        actuation_default=1.50,
        actuation_overrides={},
        rapid_trigger_default=RapidTriggerSpec(
            enabled=False, press_mm=0.50, release_mm=0.50),
        rapid_trigger_overrides={},
        key_colors=[[0, 0, 0] for _ in range(TOTAL_KEYS)],
        light={"effect": 0, "speed": 0, "r": 0, "g": 0, "b": 0, "brightness": 0},
        advanced_keys=[
            {"index": i, "mode": "NONE", "id": 0, "rs_apc_lv": 0, "gapc_sw": 0,
             "rt_sw": 0, "key1_row": 0, "key1_col": 0, "key2_row": 0,
             "key2_col": 0, "layer": 0}
            for i in range(ADVANCED_KEY_SLOTS)
        ],
        performance={"wasd_switch": 0, "mac_switch": 0, "win_lock": 0,
                     "nkro_switch": 0, "rgb_area": 0, "bottom_optimize": 0,
                     "game_mode": 0},
    )


def ensure_default_profile(profile_dir: Path) -> bool:
    """Create the default profile if the profiles directory is empty.

    Returns True if it was created. Only fires when there are no profiles at
    all, so it never reappears after you delete it deliberately alongside others.
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    if any(profile_dir.glob("*.json")):
        return False
    default_profile().save(profile_dir / f"{DEFAULT_PROFILE_NAME}.json")
    return True


def _most_common(values: Iterable):
    """The most frequent value, used to pick a profile's default."""
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]
