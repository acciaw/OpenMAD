"""Wire protocol for the DuckBread onboard configuration interface.

The keyboard speaks the VIA raw-HID protocol (32-byte reports, byte 0 is the
command ID) plus a vendor "custom channel" 0x96 that carries all the
Hall-effect features: per-key actuation point, rapid trigger, dead band, DKS,
SOCD/OKS, calibration and IAP.

Derived from the official web configurator's DuckBread controller; see
research/notes/protocol.md for provenance. The report sizes here are confirmed
against the device's own HID report descriptor, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

VENDOR_ID = 0x373B
PRODUCT_ID = 0x1058
BOOTLOADER_PRODUCT_ID = 0x2024

# The protocol version this driver was reverse engineered against and
# confirmed on hardware. The keyboard reports its own value for
# id_get_protocol_version (Cmd 0x01), and every packet layout, byte offset
# and enum in this file was derived from firmware reporting 9.
#
# This is not a formality. The vendor's own app version gates on a very
# similar number (advancedKeyVersion in its device table) to decide whether
# advanced keys are even offered, so a version bump changing the wire format
# is a real thing that happens, not a hypothetical. A future firmware update
# delivered through the official flashing tool could change offsets, add or
# remove vendor sub-commands, or reshape the lighting table, and nothing in
# this file would notice on its own. If the live value ever stops matching
# this constant, treat every packet layout below as unverified for that
# firmware until it has been checked against the device again.
KNOWN_PROTOCOL_VERSION = 9

# Raw-HID config interface (QMK/VIA convention).
USAGE_PAGE = 0xFF60
USAGE = 0x61

# Confirmed from the HID report descriptor: Report Count 0x20, Report Size 8,
# for both the Input (usage 0x62) and Output (usage 0x63) items.
REPORT_SIZE = 0x20  # 32 bytes

# Byte 0 is the command ID; the command payload starts at byte 1.
PAYLOAD_OFFSET = 0x01
MAX_PAYLOAD = REPORT_SIZE - PAYLOAD_OFFSET  # 31

# Buffer commands carry a 3-byte sub-header (offset u16be, size u8) before data.
BUFFER_HEADER = 3
MAX_BUFFER_CHUNK = MAX_PAYLOAD - BUFFER_HEADER  # 28, matches the app's 0x1C


class Cmd(IntEnum):
    """VIA command IDs (byte 0). Mirrors enum Mu in the web bundle."""

    GET_PROTOCOL_VERSION = 0x01
    GET_KEYBOARD_VALUE = 0x02
    SET_KEYBOARD_VALUE = 0x03
    DYNAMIC_KEYMAP_GET_KEYCODE = 0x04
    DYNAMIC_KEYMAP_SET_KEYCODE = 0x05
    DYNAMIC_KEYMAP_RESET = 0x06
    CUSTOM_SET_VALUE = 0x07
    CUSTOM_GET_VALUE = 0x08
    CUSTOM_SAVE = 0x09
    EEPROM_RESET = 0x0A
    BOOTLOADER_JUMP = 0x0B
    DYNAMIC_KEYMAP_MACRO_GET_COUNT = 0x0C
    DYNAMIC_KEYMAP_MACRO_GET_BUFFER_SIZE = 0x0D
    DYNAMIC_KEYMAP_MACRO_GET_BUFFER = 0x0E
    DYNAMIC_KEYMAP_MACRO_SET_BUFFER = 0x0F
    DYNAMIC_KEYMAP_MACRO_RESET = 0x10
    DYNAMIC_KEYMAP_GET_LAYER_COUNT = 0x11
    DYNAMIC_KEYMAP_GET_BUFFER = 0x12
    DYNAMIC_KEYMAP_SET_BUFFER = 0x13
    VIAL_PREFIX = 0xFE
    UNHANDLED = 0xFF


# Vendor custom channel. Rides on GET_KEYBOARD_VALUE / SET_KEYBOARD_VALUE:
# byte 1 = CUSTOM_CHANNEL, byte 2 = a Vendor sub-command.
CUSTOM_CHANNEL = 0x96

# Unsolicited telemetry reports start with these three bytes, then a type
# discriminator at byte 3. From the bundle:
#   V(D, "dataReporting", [id_get_keyboard_value, customId, dataReporting])
DATA_REPORTING_PREFIX = bytes((0x02, CUSTOM_CHANNEL, 0x14))


class Vendor(IntEnum):
    """Vendor sub-commands on channel 0x96. Mirrors enum Bu."""

    TAP_DANCE_GET = 0x01
    TAP_DANCE_SET = 0x02
    ENTRY_OP = 0x0D  # same value as BUFFER_TOG_TH; disambiguated by command
    ONE_TOG_TH = 0x03          # single-key actuation point
    CALIBRATE = 0x08
    REALTIME_ADC_AXLE = 0x09
    REALTIME_TRIP_AXLE = 0x0A
    DEAD_BAND = 0x0B
    BUFFER_TOG_TH = 0x0D       # bulk actuation points (aka entryOp)
    BUFFER_RT = 0x0E           # bulk rapid trigger
    DKS = 0x0F                 # dynamic keystroke
    ONE_RT = 0x07              # single-key rapid trigger
    MIX_AXLE = 0x10
    FEATURE = 0x11
    SAVE_LAMPLIGHT = 0x12
    LAYER = 0x13
    DATA_REPORTING = 0x14
    RESET_ALL = 0x15
    REALTIME_ADC_AXLE_BUFFER = 0x16
    REALTIME_TRIP_AXLE_BUFFER = 0x17
    CALIBRATION_START = 0x18
    CALIBRATION_FINISH = 0x19
    COMPLETE_STATUS_BUFFER = 0x1B
    ADC_TRIP_COMP_STATUS_BUFFER = 0x1C
    BOTTOM_OPTIMIZE_SWITCH = 0x1D
    GAME_MODE = 0x1E
    CALIBRATION = 0x1F
    RS = 0x20
    BOX_LIGHT = 0x21
    LIGHT_INFO = 0x41
    SET_CUSTOM_LAMPLIGHT = 0x42
    GET_CUSTOM_LAMPLIGHT = 0x45
    IAP = 0x98                 # in-application programming (firmware flash)


class SubField(IntEnum):
    """Access verb used by some vendor packets. Mirrors enum Fu/Nu."""

    GET = 0x01
    GET_BUFFER = 0x02
    SET = 0x03
    SET_BUFFER = 0x04


class FlashOp(IntEnum):
    """What a bulk vendor write should do with non-volatile memory.

    Mirrors enum Eu. This is the mechanism that makes per-application profile
    switching safe: NORMAL applies the values without touching flash, so
    switching profiles on every alt-tab costs no write endurance. Only an
    explicit user-initiated save should use ERASE_AND_WRITE.
    """

    NORMAL = 0x00           # apply only; does not commit to flash
    ERASE = 0x01
    WRITE = 0x02
    ERASE_AND_WRITE = 0x03  # persist across power cycle


# Bulk (buffer) vendor packet geometry, V2 layouts.
#   request/reply: [cmd, 0x96, sub, _, _, off_hi, off_lo, size, ...]
#   read  data at raw[8]
#   write flashOp at raw[8], data at raw[9]
BULK_OFFSET_AT = 5   # uint16 big-endian
BULK_SIZE_AT = 7     # number of KEYS, not bytes
BULK_READ_DATA_AT = 8
BULK_FLASH_OP_AT = 8
BULK_WRITE_DATA_AT = 9

ACTUATION_ENTRY_V2 = 2       # apc uint16be
RAPID_TRIGGER_ENTRY_V2 = 5   # on u8, release u16be, press u16be

# Keys per packet, derived from the 32-byte report size.
MAX_ACTUATION_READ = (REPORT_SIZE - BULK_READ_DATA_AT) // ACTUATION_ENTRY_V2
MAX_ACTUATION_WRITE = (REPORT_SIZE - BULK_WRITE_DATA_AT) // ACTUATION_ENTRY_V2
MAX_RT_READ = (REPORT_SIZE - BULK_READ_DATA_AT) // RAPID_TRIGGER_ENTRY_V2
MAX_RT_WRITE = (REPORT_SIZE - BULK_WRITE_DATA_AT) // RAPID_TRIGGER_ENTRY_V2


class KeyboardValue(IntEnum):
    """Standard VIA id_get/set_keyboard_value sub-IDs.

    These are VIA's own IDs, confirmed against the hardware: querying 0x01
    returns a monotonically increasing millisecond counter (uptime), not a
    layer number.

    Note: the web bundle's enum Gu (defaultLayerSt / swapWasdSt / macosSt /
    winLockSt / nKroSt) is a *UI* state enum and is NOT this ID space. Treating
    it as such is what made 0x01 look like "default layer".
    """

    UPTIME = 0x01
    LAYOUT_OPTIONS = 0x02
    SWITCH_MATRIX_STATE = 0x03
    FIRMWARE_VERSION = 0x04
    DEVICE_INDICATION = 0x05


# Keyboard values that change on their own and must never be treated as
# configuration: uptime ticks, and switch matrix state which reflects which
# keys are physically held down right now.
VOLATILE_KEYBOARD_VALUES = frozenset(
    {KeyboardValue.UPTIME, KeyboardValue.SWITCH_MATRIX_STATE}
)


class AdvancedKeyMode(IntEnum):
    """Per-key advanced mode. Mirrors enum Vu."""

    NONE = 0x00
    RS = 0x01
    SOCD = 0x02
    SOCD_KEY1 = 0x03
    SOCD_KEY2 = 0x04
    SOCD_BALANCE = 0x05
    OKS = 0x06


class MacroAction(IntEnum):
    """Macro byte-stream opcodes. Mirrors enum Ru."""

    SS_TAP_CODE = 0x01
    SS_DOWN_CODE = 0x02
    SS_UP_CODE = 0x03
    SS_DELAY_CODE = 0x04
    VIAL_MACRO_EXT_TAP = 0x05
    VIAL_MACRO_EXT_DOWN = 0x06
    VIAL_MACRO_EXT_UP = 0x07


# Safety classification. Enforced in device.py, not merely documented.

# VIA commands that mutate persistent state.
WRITE_COMMANDS = frozenset(
    {
        Cmd.SET_KEYBOARD_VALUE,
        Cmd.DYNAMIC_KEYMAP_SET_KEYCODE,
        Cmd.DYNAMIC_KEYMAP_SET_BUFFER,
        Cmd.DYNAMIC_KEYMAP_MACRO_SET_BUFFER,
        Cmd.CUSTOM_SET_VALUE,
        Cmd.CUSTOM_SAVE,
        Cmd.DYNAMIC_KEYMAP_RESET,
        Cmd.DYNAMIC_KEYMAP_MACRO_RESET,
        Cmd.EEPROM_RESET,
        Cmd.BOOTLOADER_JUMP,
    }
)

# VIA commands that wipe onboard memory or leave the keyboard unusable until
# reflashed. Never sent without an explicit, separate opt-in.
DANGEROUS_COMMANDS = frozenset(
    {
        Cmd.DYNAMIC_KEYMAP_RESET,
        Cmd.DYNAMIC_KEYMAP_MACRO_RESET,
        Cmd.EEPROM_RESET,
        Cmd.BOOTLOADER_JUMP,
    }
)

# Vendor sub-commands that are destructive regardless of get/set framing.
# Calibration is singled out because bad calibration data is the one thing that
# materially degrades a Hall-effect board, and IAP starts a firmware flash.
DANGEROUS_VENDOR = frozenset(
    {
        Vendor.CALIBRATE,
        Vendor.CALIBRATION_START,
        Vendor.CALIBRATION_FINISH,
        Vendor.RESET_ALL,
        Vendor.IAP,
    }
)


# Geometry
#
# These are DEFAULTS, not facts about the connected keyboard. The controller is
# shared across boards whose matrices are 5x15, 5x14 and 5x6 (see devices.py),
# and every buffer on the wire -- the keymap, the actuation array, the per-key
# colour table -- is packed row-major over the *board's own* column count. A
# 60% board packs 14 keys per row, so reading its keymap with a stride of 15
# lands one key further left on every row after the first.
#
# So anything holding a live Mad68 must take its geometry from `kb.spec`
# (kb.matrix_rows / kb.matrix_cols / kb.total_keys / kb.keymap_size) rather
# than from these names. They exist for code with no device in hand, and are
# the 5x15 board this driver was developed against.
LAYER_COUNT = 4
MATRIX_ROWS = 5
MATRIX_COLS = 15
KEYCODE_SIZE = 2  # keyClass, keyId, a 16-bit QMK keycode, big-endian
KEYMAP_SIZE = LAYER_COUNT * MATRIX_ROWS * MATRIX_COLS * KEYCODE_SIZE  # 600
TOTAL_KEYS = MATRIX_ROWS * MATRIX_COLS  # 75


def keymap_size(rows: int = MATRIX_ROWS, cols: int = MATRIX_COLS,
                layers: int = LAYER_COUNT) -> int:
    """Bytes in the dynamic keymap buffer for a given matrix."""
    return layers * rows * cols * KEYCODE_SIZE


def keymap_offset(layer: int, row: int, col: int, rows: int = MATRIX_ROWS,
                  cols: int = MATRIX_COLS) -> int:
    """Byte offset of one keycode inside a dynamic keymap buffer."""
    return ((layer * rows + row) * cols + col) * KEYCODE_SIZE

# Byte order the LEDs expect. Addressable LEDs are commonly GRB rather than
# RGB, and the firmware passes our bytes through untouched, the value read
# back is always whatever was written, so a wrong order is invisible over the
# wire and only shows up as wrong colours on the keyboard.
# 
# Set this to whatever tools/led_order.py reports. "RGB" means no reordering.
LED_CHANNEL_ORDER = "RGB"

_ORDERS = {
    "RGB": (0, 1, 2), "RBG": (0, 2, 1), "GRB": (1, 0, 2),
    "GBR": (1, 2, 0), "BRG": (2, 0, 1), "BGR": (2, 1, 0),
}


# Per-channel gain applied on the way to the LEDs, as (red, green, blue).
# 
# The three colours are not equally bright on this board: red works on its own
# and alongside blue, but is overwhelmed whenever green is strong, yellow
# renders green, white renders cyan. That is ordinary LED physics (red dice
# have a lower forward voltage, and the eye is far more sensitive to green),
# not a protocol problem, so it is corrected here rather than worked around in
# the UI.
# 
# The correction holds green and blue back rather than pushing red up: in white
# and yellow red is already at 255, so there is no headroom to give it. Expect
# something like (1.0, 0.6, 0.7), the cost is a dimmer board overall.
# 
# Run tools/led_gain.py to tune it by eye. (1.0, 1.0, 1.0) disables it.
LED_CHANNEL_GAIN = (1.0, 0.23, 0.27)


# Highest brightness the firmware will accept in a LightInfo write.
# 
# This is not a soft limit. A LightInfo packet carrying a higher value has its
# brightness field dropped, the effect, speed and colour in the same packet
# still apply, so the write looks successful while the board silently keeps the
# old brightness. Measured on the device: 210 is stored, 211 and above are not.
# 
# The stock web app carries the same number as a per-model override keyed on
# the device's custom.name, {"MAD 68 RGB": 0xd2, ..., "default": 0xc8} --
# see research/notes/protocol.md §7.
BRIGHTNESS_MAX = 210


# Lighting effects that animate, and so need a speed in 1..255.
# 
# Taken from the stock app's own effect table (list Bt), where an effect
# carries a speed: {min: 1, max: 255} entry only if it moves. The rest are
# static by design, 4 and 5 are "Rainbow *static*", a still image.
# 
# Speed 0 is out of range. The firmware accepts it and freezes the animation on
# its first frame, which is indistinguishable from the effect never applying.
ANIMATED_EFFECTS = frozenset({6, 7, 8, 9, 11, 13, 19, 23, 26, 28, 31, 36, 39})

# Speed to use when an animated effect is asked for with no usable speed.
DEFAULT_EFFECT_SPEED = 128

# Lighting effects that take a single colour from the LightInfo packet.
#
# From the same stock table as ANIMATED_EFFECTS: an effect is listed here when
# it carries a colour entry. 1 (Customization) is deliberately absent -- it
# renders the per-key colour table instead, so the packet's colour is not what
# you see, and forcing one would override the user's own key colours.
COLOUR_EFFECTS = frozenset({2, 3, 4, 5, 6, 7, 8, 9, 11, 26, 28, 31, 36})

# Colour to use when a colour-driven effect is asked for without one set.
#
# Black is not something the LEDs can render. It collapses these effects to a
# single dead hue, which is what made the two Rainbow static modes stop looking
# like rainbows in any profile where a colour had never been picked.
DEFAULT_EFFECT_COLOUR = (255, 255, 255)


def _apply_gain(value: int, gain: float) -> int:
    return max(0, min(255, round(value * gain)))


def encode_rgb(r: int, g: int, b: int) -> bytes:
    """Apply channel gain, then reorder into the order the LEDs expect."""
    gr, gg, gb = LED_CHANNEL_GAIN
    src = (_apply_gain(r, gr), _apply_gain(g, gg), _apply_gain(b, gb))
    return bytes(src[i] for i in _ORDERS[LED_CHANNEL_ORDER])


def decode_rgb(data: bytes) -> tuple[int, int, int]:
    """Inverse of encode_rgb: wire order and gain back to logical RGB."""
    order = _ORDERS[LED_CHANNEL_ORDER]
    wire = [0, 0, 0]
    for wire_pos, logical in enumerate(order):
        wire[logical] = data[wire_pos]
    gains = LED_CHANNEL_GAIN
    return tuple(  # type: ignore[return-value]
        max(0, min(255, round(v / g))) if g else v for v, g in zip(wire, gains)
    )


# Travel values (actuation point, rapid-trigger press/release) are uint16
# big-endian in units of 0.01 mm on this keyboard. This is the V2 scale; V1 used
# a single byte at 0.02 mm. DuckBread.isV2 is hardcoded true, so V2 always
# applies here, see protocol.md §4.
TRAVEL_UNIT_MM = 0.01
TRAVEL_MAX_RAW = 0xFFFF


def travel_to_mm(raw: int) -> float:
    """Decode a raw uint16 travel value to millimetres."""
    return round(raw * TRAVEL_UNIT_MM, 2)


def mm_to_travel(mm: float) -> int:
    """Encode millimetres to a raw uint16 travel value, clamped to range."""
    return max(0, min(TRAVEL_MAX_RAW, round(mm / TRAVEL_UNIT_MM)))


def travel_bytes(mm: float) -> bytes:
    """Encode millimetres as the on-wire uint16 big-endian pair."""
    return mm_to_travel(mm).to_bytes(2, "big")


def travel_from_bytes(data: bytes) -> float:
    """Decode an on-wire uint16 big-endian pair to millimetres."""
    return travel_to_mm(int.from_bytes(data, "big"))


# Packet construction


def build(cmd: Cmd, payload: bytes = b"") -> bytes:
    """Build a 32-byte host to device report. No checksum; VIA has none."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} exceeds {MAX_PAYLOAD}")
    buf = bytearray(REPORT_SIZE)
    buf[0] = int(cmd)
    buf[PAYLOAD_OFFSET:PAYLOAD_OFFSET + len(payload)] = payload
    return bytes(buf)


def build_vendor(cmd: Cmd, sub: Vendor, payload: bytes = b"") -> bytes:
    """Build a vendor custom-channel report: [cmd, 0x96, sub, ...payload]."""
    return build(cmd, bytes([CUSTOM_CHANNEL, int(sub)]) + payload)


def build_buffer_read(cmd: Cmd, offset: int, size: int) -> bytes:
    """Build a VIA buffer read: [cmd, offset_hi, offset_lo, size]."""
    if size > MAX_BUFFER_CHUNK:
        raise ValueError(f"size {size} exceeds {MAX_BUFFER_CHUNK}")
    return build(cmd, offset.to_bytes(2, "big") + bytes([size]))


def build_buffer_write(cmd: Cmd, offset: int, data: bytes) -> bytes:
    """Build a VIA buffer write: [cmd, offset_hi, offset_lo, size, ...data]."""
    if len(data) > MAX_BUFFER_CHUNK:
        raise ValueError(f"data {len(data)} exceeds {MAX_BUFFER_CHUNK}")
    return build(cmd, offset.to_bytes(2, "big") + bytes([len(data)]) + data)


@dataclass(frozen=True)
class Reply:
    """A parsed device to host report."""

    raw: bytes

    @property
    def command_id(self) -> int:
        return self.raw[0]

    @property
    def payload(self) -> bytes:
        return self.raw[PAYLOAD_OFFSET:]

    @property
    def buffer_data(self) -> bytes:
        """Data portion of a buffer reply, after the 3-byte offset/size header."""
        size = self.raw[PAYLOAD_OFFSET + 2]
        start = PAYLOAD_OFFSET + BUFFER_HEADER
        return self.raw[start:start + size]

    @property
    def is_unhandled(self) -> bool:
        """VIA firmwares answer an unknown command by echoing it with 0xFF."""
        return self.command_id == int(Cmd.UNHANDLED)

    @property
    def is_data_reporting(self) -> bool:
        """True for an unsolicited telemetry push report (02 96 14 ...).

        The web bundle identifies these by comparing the first three bytes
        against [id_get_keyboard_value, customId, dataReporting].
        """
        return len(self.raw) > 3 and self.raw[0:3] == DATA_REPORTING_PREFIX

    @property
    def stream_type(self) -> int | None:
        """The type discriminator at byte 3 of a data-reporting report."""
        return self.raw[3] if self.is_data_reporting else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        try:
            name = Cmd(self.command_id).name
        except ValueError:
            name = f"{self.command_id:#04x}"
        return f"Reply(cmd={name}, payload={self.payload[:8].hex(' ')}...)"
