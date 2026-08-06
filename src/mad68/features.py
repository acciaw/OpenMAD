"""Structured types for the MAD68 HE's advanced features.

Layouts recovered from the web configurator's DuckBread packet classes. Byte
offsets below are into the 32-byte report (raw[...]), with the vendor channel
already accounted for; see research/notes/protocol.md for provenance.

Two different framings are in play, which is easy to get wrong:

* Vendor channel, [0x02|0x03, 0x96, sub, ...]. Used by dead band, feature
  flags, game mode, bottom optimise, box light, DKS and advanced keys.
* VIA custom/lighting value, [0x08|0x07, sub, ...] with no 0x96
  byte. Used by per-key RGB (0x45/0x42) and the global light effect (0x41).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum

from .protocol import AdvancedKeyMode, MacroAction

# Dynamic keystroke (DKS)

# A DKS entry holds four actions of four bytes each.
DKS_ACTION_COUNT = 4
DKS_ACTION_SIZE = 4
# Trigger bits per action (status is a uint16 with bits 0..9 used).
DKS_STATUS_BITS = 10


@dataclass(frozen=True)
class DksAction:
    """One DKS action: a 10-bit trigger mask plus the keycode it fires.

    status bits select which travel events fire this action. The firmware's
    exact bit semantics are not documented by the web app either, it round-trips
    the mask verbatim, so the mask is preserved rather than interpreted.
    """

    status: int
    key_class: int
    key_id: int

    @property
    def keycode(self) -> int:
        return (self.key_class << 8) | self.key_id

    @property
    def bits(self) -> list[int]:
        return [(self.status >> i) & 1 for i in range(DKS_STATUS_BITS)]

    @property
    def is_empty(self) -> bool:
        return self.status == 0 and self.keycode == 0

    def to_bytes(self) -> bytes:
        return (
            self.status.to_bytes(2, "big")
            + bytes([self.key_class & 0xFF, self.key_id & 0xFF])
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "DksAction":
        return cls(
            status=int.from_bytes(data[0:2], "big"),
            key_class=data[2],
            key_id=data[3],
        )

    @classmethod
    def from_keycode(cls, keycode: int, status: int = 0) -> "DksAction":
        return cls(status=status, key_class=(keycode >> 8) & 0xFF, key_id=keycode & 0xFF)

    def to_json(self) -> dict:
        return {"status": self.status, "keycode": self.keycode, "bits": self.bits}


@dataclass(frozen=True)
class Dks:
    """A full dynamic-keystroke entry: index plus four actions."""

    index: int
    actions: tuple[DksAction, ...]

    @property
    def is_empty(self) -> bool:
        return all(a.is_empty for a in self.actions)

    def to_bytes(self) -> bytes:
        out = bytearray()
        for i in range(DKS_ACTION_COUNT):
            a = self.actions[i] if i < len(self.actions) else DksAction(0, 0, 0)
            out += a.to_bytes()
        return bytes(out)

    @classmethod
    def from_bytes(cls, index: int, data: bytes) -> "Dks":
        actions = tuple(
            DksAction.from_bytes(data[i * DKS_ACTION_SIZE:(i + 1) * DKS_ACTION_SIZE])
            for i in range(DKS_ACTION_COUNT)
        )
        return cls(index=index, actions=actions)

    def to_json(self) -> dict:
        return {"index": self.index, "actions": [a.to_json() for a in self.actions]}


# Advanced keys: rapid snap / SOCD / OKS  (vendor rs, 0x20)


@dataclass(frozen=True)
class AdvancedKey:
    """A rapid-snap / SOCD / OKS binding.

    Field offsets are relative to the packet start:
      raw[5]  subField      raw[6]  index      raw[7]  mode      raw[8]  id
      raw[9:11] rsApcLv u16 raw[11] gapcSw     raw[12] rtSw
      raw[13] key1Row  raw[14] key1Col  raw[15] key2Row  raw[16] key2Col
      raw[17] layer
    """

    index: int
    mode: AdvancedKeyMode = AdvancedKeyMode.NONE
    id: int = 0
    rs_apc_lv: int = 0
    gapc_sw: int = 0
    rt_sw: int = 0
    key1_row: int = 0
    key1_col: int = 0
    key2_row: int = 0
    key2_col: int = 0
    layer: int = 0

    @property
    def is_active(self) -> bool:
        return self.mode != AdvancedKeyMode.NONE

    def to_json(self) -> dict:
        d = asdict(self)
        d["mode"] = self.mode.name if isinstance(self.mode, AdvancedKeyMode) else self.mode
        return d


# Lighting


@dataclass(frozen=True)
class LightInfo:
    """Global lighting: effect, speed, colour and brightness.

    Framing is id_lighting_get/set_value with the sub-id at byte 1, there is
    no 0x96 channel byte here:
      raw[1] 0x41   raw[2] effect   raw[4] speed
      raw[5:8] r,g,b               raw[8] brightness
    """

    effect: int = 0
    speed: int = 0
    r: int = 0
    g: int = 0
    b: int = 0
    brightness: int = 0

    @property
    def color_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["color_hex"] = self.color_hex
        return d


@dataclass(frozen=True)
class BoxLight:
    """Side/box lighting: raw[3] mode, raw[4] colorful, raw[5] brightness, raw[6] speed."""

    mode: int = 0
    colorful: int = 0
    brightness: int = 0
    speed: int = 0

    def to_json(self) -> dict:
        return asdict(self)


# Scalar settings


@dataclass(frozen=True)
class TapDance:
    """Hold/Click (MT): one keycode on tap, another on hold.

    Framed on VIA's vial prefix rather than the 0x96 channel:
      raw[1] entryOp 0x0D   raw[2] get 0x01 / set 0x02   raw[3] index
      raw[4:6] tap keycode  raw[6:8] hold keycode        raw[8:10] timer ms
    """

    index: int
    tap: int = 0
    hold: int = 0
    timer_ms: int = 200

    @property
    def is_active(self) -> bool:
        return bool(self.tap or self.hold)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DeadBand:
    """Sensor dead band in millimetres. raw[3:5] top, raw[5:7] bottom (u16 x0.01)."""

    top_mm: float = 0.0
    bottom_mm: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class KeyboardFeature:
    """Feature flags. raw[3] rgbArea, [4] wasd, [5] mac, [6] winLock, [7] nkro."""

    rgb_area: int = 0
    wasd_switch: int = 0
    mac_switch: int = 0
    win_lock: int = 0
    nkro_switch: int = 0

    def to_json(self) -> dict:
        return asdict(self)


# Macros

# VIA marks a non-literal action with this lead byte inside the macro buffer.
MACRO_ESCAPE = 0x01
# Macros are separated by a NUL in the shared buffer.
MACRO_TERMINATOR = 0x00

# Opcodes carrying a single 1-byte keycode.
_ONE_BYTE_CODE = {
    MacroAction.SS_TAP_CODE,
    MacroAction.SS_DOWN_CODE,
    MacroAction.SS_UP_CODE,
}
# Vial extensions carrying a 2-byte keycode.
_TWO_BYTE_CODE = {
    MacroAction.VIAL_MACRO_EXT_TAP,
    MacroAction.VIAL_MACRO_EXT_DOWN,
    MacroAction.VIAL_MACRO_EXT_UP,
}


@dataclass(frozen=True)
class MacroStep:
    """One decoded macro step."""

    kind: str            # "text" | "tap" | "down" | "up" | "delay"
    text: str = ""
    keycode: int = 0
    delay_ms: int = 0

    def to_json(self) -> dict:
        return asdict(self)


def _decode_delay(b1: int, b2: int) -> int:
    """VIA encodes a delay as two bytes each in 1..255 to avoid NUL and escapes."""
    return (b1 - 1) + (b2 - 1) * 255


def _encode_delay(ms: int) -> bytes:
    ms = max(0, min(255 * 255 + 254, ms))
    return bytes([(ms % 255) + 1, (ms // 255) + 1])


def decode_macro(data: bytes) -> list[MacroStep]:
    """Decode one macro's bytes (no terminator) into steps."""
    steps: list[MacroStep] = []
    text = ""
    i = 0
    while i < len(data):
        b = data[i]
        if b != MACRO_ESCAPE:
            text += chr(b)
            i += 1
            continue

        if text:
            steps.append(MacroStep("text", text=text))
            text = ""
        if i + 1 >= len(data):
            break
        op = data[i + 1]
        try:
            action = MacroAction(op)
        except ValueError:
            i += 2
            continue

        if action in _ONE_BYTE_CODE and i + 2 < len(data):
            kind = {MacroAction.SS_TAP_CODE: "tap",
                    MacroAction.SS_DOWN_CODE: "down",
                    MacroAction.SS_UP_CODE: "up"}[action]
            steps.append(MacroStep(kind, keycode=data[i + 2]))
            i += 3
        elif action in _TWO_BYTE_CODE and i + 3 < len(data):
            kind = {MacroAction.VIAL_MACRO_EXT_TAP: "tap",
                    MacroAction.VIAL_MACRO_EXT_DOWN: "down",
                    MacroAction.VIAL_MACRO_EXT_UP: "up"}[action]
            steps.append(MacroStep(kind, keycode=data[i + 2] | (data[i + 3] << 8)))
            i += 4
        elif action is MacroAction.SS_DELAY_CODE and i + 3 < len(data):
            steps.append(MacroStep("delay", delay_ms=_decode_delay(data[i + 2], data[i + 3])))
            i += 4
        else:
            i += 2

    if text:
        steps.append(MacroStep("text", text=text))
    return steps


def encode_macro(steps: list[MacroStep]) -> bytes:
    """Encode steps back into one macro's bytes (no terminator)."""
    out = bytearray()
    for s in steps:
        if s.kind == "text":
            for ch in s.text:
                code = ord(ch)
                if code in (MACRO_ESCAPE, MACRO_TERMINATOR) or code > 0x7F:
                    raise ValueError(
                        f"macro text cannot contain byte {code:#04x}; use a tap step"
                    )
                out.append(code)
        elif s.kind == "delay":
            out += bytes([MACRO_ESCAPE, int(MacroAction.SS_DELAY_CODE)])
            out += _encode_delay(s.delay_ms)
        elif s.kind in ("tap", "down", "up"):
            if s.keycode > 0xFF:
                op = {"tap": MacroAction.VIAL_MACRO_EXT_TAP,
                      "down": MacroAction.VIAL_MACRO_EXT_DOWN,
                      "up": MacroAction.VIAL_MACRO_EXT_UP}[s.kind]
                out += bytes([MACRO_ESCAPE, int(op),
                              s.keycode & 0xFF, (s.keycode >> 8) & 0xFF])
            else:
                op = {"tap": MacroAction.SS_TAP_CODE,
                      "down": MacroAction.SS_DOWN_CODE,
                      "up": MacroAction.SS_UP_CODE}[s.kind]
                out += bytes([MACRO_ESCAPE, int(op), s.keycode & 0xFF])
        else:
            raise ValueError(f"unknown macro step kind {s.kind!r}")
    return bytes(out)


def split_macro_buffer(buffer: bytes, count: int) -> list[bytes]:
    """Split the shared macro buffer into count NUL-separated macros."""
    macros: list[bytes] = []
    start = 0
    for _ in range(count):
        end = buffer.find(bytes([MACRO_TERMINATOR]), start)
        if end == -1:
            macros.append(buffer[start:])
            break
        macros.append(buffer[start:end])
        start = end + 1
    while len(macros) < count:
        macros.append(b"")
    return macros


def join_macro_buffer(macros: list[bytes], size: int) -> bytes:
    """Pack macros back into a fixed-size NUL-separated buffer."""
    out = bytearray()
    for m in macros:
        out += m
        out.append(MACRO_TERMINATOR)
    if len(out) > size:
        raise ValueError(f"macros need {len(out)} bytes, buffer is {size}")
    return bytes(out) + bytes(size - len(out))
