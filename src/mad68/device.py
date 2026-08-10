"""HID transport for the DuckBread config interface (VIA protocol).

Safety model: the handle is read-only unless constructed with writes=True, and
destructive commands (EEPROM reset, keymap reset, bootloader jump, calibration,
IAP) additionally need dangerous=True. The gate lives in transfer(), the
single chokepoint every command passes through, so no caller can route around
it, including vendor sub-commands smuggled inside a read-shaped packet.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Iterator

import hid

from . import devices
from .protocol import (
    ACTUATION_ENTRY_V2,
    BOOTLOADER_PRODUCT_ID,
    BRIGHTNESS_MAX,
    BULK_FLASH_OP_AT,
    BULK_OFFSET_AT,
    BULK_READ_DATA_AT,
    BULK_SIZE_AT,
    BULK_WRITE_DATA_AT,
    CUSTOM_CHANNEL,
    DANGEROUS_COMMANDS,
    DANGEROUS_VENDOR,
    KEYCODE_SIZE,
    KNOWN_PROTOCOL_VERSION,
    MAX_ACTUATION_READ,
    MAX_ACTUATION_WRITE,
    MAX_BUFFER_CHUNK,
    MAX_PAYLOAD,
    MAX_RT_READ,
    MAX_RT_WRITE,
    PRODUCT_ID,
    RAPID_TRIGGER_ENTRY_V2,
    REPORT_SIZE,
    USAGE,
    USAGE_PAGE,
    VENDOR_ID,
    WRITE_COMMANDS,
    AdvancedKeyMode,
    Cmd,
    FlashOp,
    Reply,
    SubField,
    Vendor,
    build,
    build_buffer_read,
    build_buffer_write,
    build_vendor,
    decode_rgb,
    encode_rgb,
    keymap_offset,
    keymap_size,
    travel_bytes,
    travel_from_bytes,
)
from .features import (
    DKS_ACTION_COUNT,
    DKS_ACTION_SIZE,
    AdvancedKey,
    BoxLight,
    DeadBand,
    Dks,
    KeyboardFeature,
    LightInfo,
    MacroStep,
    TapDance,
    decode_macro,
    encode_macro,
    join_macro_buffer,
    split_macro_buffer,
)


class Mad68Error(RuntimeError):
    pass


class DeviceNotFound(Mad68Error):
    pass


class WriteBlocked(Mad68Error):
    """A mutating or destructive command was attempted without the right gate."""


@dataclass(frozen=True)
class RapidTrigger:
    """Per-key rapid-trigger configuration (vendor ONE_RT, V2 layout)."""

    row: int
    col: int
    enabled: bool
    release_mm: float
    press_mm: float

    def to_json(self) -> dict:
        return asdict(self)


def find_interfaces() -> list[dict]:
    """All HID interfaces exposed by the keyboard, for display."""
    return sorted(
        hid.enumerate(VENDOR_ID, 0),
        key=lambda d: (d.get("product_id", 0), d.get("interface_number", -1),
                       d.get("usage_page", 0)),
    )


def in_bootloader() -> bool:
    """True if any known board is sitting in its bootloader.

    Every board has its own bootloader PID, so the whole set has to be
    considered -- but as a single vendor-wide enumeration filtered in Python,
    not one call per ID. See find_config_interface for why.
    """
    return any(int(d.get("product_id") or 0) in devices.BOOTLOADER_IDS
               for d in hid.enumerate(VENDOR_ID, 0))


def find_config_interface(product_id: int | None = None) -> dict:
    """The raw-HID config interface (usage page 0xFF60, usage 0x61).

    With no product_id, any board in the registry will do and the lowest ID
    present wins. Pinning to a single PID was what limited this driver to one
    keyboard: the config interface is a property of the controller, and the
    controller is shared across the whole family.

    One vendor-wide enumeration, filtered here, rather than one call per
    registered ID. The obvious version -- looping hid.enumerate over all 25
    PIDs -- measured 35 ms against 8.5 ms for the single call, and the switcher
    opens the device on every profile apply, so that would have made every
    supported board pay four times over for the privilege of supporting the
    other twenty-four.
    """
    candidates = [
        d
        for d in hid.enumerate(VENDOR_ID, 0)
        if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE
        and (product_id is None
             or int(d.get("product_id") or 0) == product_id)
        and (product_id is not None
             or int(d.get("product_id") or 0) in devices.BY_PRODUCT_ID)
    ]

    if not candidates:
        if in_bootloader():
            raise DeviceNotFound(
                "keyboard is in BOOTLOADER mode -- unplug and replug it to "
                "return to normal operation."
            )
        raise DeviceNotFound(
            f"no interface with usage page {USAGE_PAGE:#06x} / usage {USAGE:#04x} "
            f"for any known board on {VENDOR_ID:#06x}. Is the keyboard connected "
            f"directly, rather than through a hub or KVM that filters HID?"
        )

    # More than one board plugged in at once is legitimate; the driver just has
    # to pick one deterministically rather than fail.
    candidates.sort(key=lambda c: (c.get("product_id", 0), str(c.get("path"))))
    return candidates[0]


class Mad68:
    """A connected DuckBread-family config interface."""

    def __init__(self, *, writes: bool = False, dangerous: bool = False,
                 timeout_ms: int = 1000, product_id: int | None = None,
                 allow_unverified: bool = False):
        self._writes = writes
        self._dangerous = dangerous
        self._timeout_ms = timeout_ms
        self._product_id = product_id
        # Firmware whose protocol version this driver has not been checked
        # against is read-only unless the caller says otherwise. See the gate in
        # transfer() for why reads are exempt.
        self._allow_unverified = allow_unverified
        self._dev: hid.device | None = None
        self.info: dict = {}
        self._populated: list[bool] | None = None
        # Filled in by open(): which board this is, and what firmware it runs.
        # Cached privately because protocol_version() is a method on this class
        # and callers already depend on it.
        self.spec: devices.BoardSpec = devices.DEFAULT_BOARD
        self._protocol_version: int | None = None

    # geometry
    #
    # Read from the connected board rather than from module constants, because
    # the family spans 5x14, 5x15 and 5x6 matrices. The module constants remain
    # as the defaults for code that has no device in hand.

    @property
    def total_keys(self) -> int:
        return self.spec.total_keys

    @property
    def matrix_rows(self) -> int:
        return self.spec.rows

    @property
    def matrix_cols(self) -> int:
        return self.spec.cols

    @property
    def layer_count_spec(self) -> int:
        """Layers this board's matrix is packed for, from the registry."""
        return self.spec.layers

    @property
    def keymap_size(self) -> int:
        """Size of this board's dynamic keymap buffer, in bytes."""
        return keymap_size(self.spec.rows, self.spec.cols, self.spec.layers)

    @property
    def firmware_version(self) -> int | None:
        """Protocol version read at open, or None if the board did not answer."""
        return self._protocol_version

    @property
    def firmware_verified(self) -> bool:
        """Whether this firmware reports the protocol version we were built on."""
        return self._protocol_version == KNOWN_PROTOCOL_VERSION

    # lifecycle

    def open(self) -> "Mad68":
        iface = find_config_interface(self._product_id)
        dev = hid.device()
        dev.open_path(iface["path"])
        dev.set_nonblocking(0)
        self._dev = dev
        self.info = iface
        pid = int(iface.get("product_id") or 0)
        self.spec = devices.describe(pid)
        # A board the user has deliberately unlocked writes like a verified one.
        # Consulted here rather than passed in, so every entry point -- HUD,
        # tray switcher, CLI -- honours the same decision without plumbing.
        if devices.is_unlocked(pid):
            self._allow_unverified = True

        # One extra packet at open, so the write gate has something to decide
        # on. Cheap next to the round trips any real operation costs, and it
        # has to happen before the first write rather than after it. A board
        # that will not answer stays None, which the gate treats as unverified.
        try:
            self._protocol_version = self.protocol_version()
        except Exception:
            self._protocol_version = None
        return self

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def __enter__(self) -> "Mad68":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def _handle(self) -> hid.device:
        if self._dev is None:
            raise Mad68Error("device is not open")
        return self._dev

    # safety gate

    def _check(self, packet: bytes) -> None:
        """Reject anything this handle is not authorised to send."""
        cmd = packet[0]
        try:
            cmd = Cmd(cmd)
        except ValueError:
            raise Mad68Error(f"refusing to send unknown command {cmd:#04x}")

        # A vendor sub-command can be destructive even in a "get" packet.
        vendor_sub = None
        if cmd in (Cmd.GET_KEYBOARD_VALUE, Cmd.SET_KEYBOARD_VALUE) and len(packet) > 2:
            if packet[1] == CUSTOM_CHANNEL:
                try:
                    vendor_sub = Vendor(packet[2])
                except ValueError:
                    vendor_sub = None

        if vendor_sub is not None and vendor_sub in DANGEROUS_VENDOR and not self._dangerous:
            raise WriteBlocked(
                f"vendor sub-command {vendor_sub.name} is destructive "
                f"(calibration / reset / firmware flash) and needs "
                f"Mad68(writes=True, dangerous=True)."
            )
        # Tap dance rides on the vial prefix rather than a set-shaped command,
        # so its write would otherwise sail past the read-only gate.
        if (cmd is Cmd.VIAL_PREFIX and len(packet) > 2
                and packet[1] == int(Vendor.ENTRY_OP)
                and packet[2] == int(Vendor.TAP_DANCE_SET) and not self._writes):
            raise WriteBlocked(
                "writing a hold/click (tap dance) binding mutates onboard memory "
                "and this handle is read-only. Construct Mad68(writes=True)."
            )

        if cmd in DANGEROUS_COMMANDS and not self._dangerous:
            raise WriteBlocked(
                f"{cmd.name} wipes onboard state or reboots to bootloader; "
                f"needs Mad68(writes=True, dangerous=True)."
            )

        # Anything unconfirmed is read-only.
        #
        # Two separate questions have to both come out yes, because they are
        # different kinds of evidence:
        #
        #   firmware_verified -- does this board report the protocol version
        #       every packet layout in protocol.py was derived from? A
        #       different version means the wire format may have moved.
        #   spec.verified -- has this *model* actually been run against this
        #       driver by someone? The board list came out of the vendor's
        #       JavaScript bundle, so a matching protocol version is a strong
        #       inference that the layouts hold, but it is still an inference.
        #       It says nothing about LED count, which vendor sub-commands the
        #       model implements, or anything else that varies per board.
        #
        # Reads are exempt on purpose. Reading a wrong offset costs nothing --
        # the worst case is a value displayed as nonsense, which is visible and
        # harmless. Writing a wrong offset puts arbitrary bytes into whichever
        # setting actually lives there. So an unconfirmed board stays fully
        # inspectable and diagnosable, which is what makes a useful report
        # possible, while being unable to damage anything.
        #
        # A board that did not answer the version query at all is unverified
        # too -- silence is not evidence of a match.
        #
        # The user can lift this per board from Other Settings; that path sets
        # allow_unverified via devices.is_unlocked() and asks for a typed
        # confirmation first.
        if (cmd in WRITE_COMMANDS
                and self._writes
                and not self._allow_unverified
                and not (self.firmware_verified and self.spec.verified)):
            got = ("no answer" if self._protocol_version is None
                   else str(self._protocol_version))
            if not self.firmware_verified:
                why = (f"it reports protocol version {got}, and this driver's "
                       f"packet layouts were only confirmed against version "
                       f"{KNOWN_PROTOCOL_VERSION}")
            else:
                why = (f"the {self.spec.name} has never been run against this "
                       f"driver -- its support is inferred from sharing a "
                       f"controller with a board that has")
            raise WriteBlocked(
                f"{cmd.name} refused: {why}. Reading is safe and stays enabled. "
                f"Enable writing for this board in Other Settings, or send a "
                f"board report so it can be supported properly."
            )

        if cmd in WRITE_COMMANDS and not self._writes:
            raise WriteBlocked(
                f"{cmd.name} mutates onboard memory and this handle is read-only. "
                f"Construct Mad68(writes=True) to allow it."
            )

    # raw transfer

    def send(self, packet: bytes, *, expect_reply: bool = True) -> Reply | None:
        """Send one pre-built 32-byte report and return the matching reply.

        Matching is not on byte 0 alone. Vendor traffic and the telemetry push
        stream both use command 0x02: a vendor read reply is 02 96 <sub> ...
        while a push report is 02 96 14 .... Matching byte 0 only would let a
        push report satisfy an unrelated vendor read once streaming is on, so a
        vendor packet additionally matches its channel and sub-command bytes.
        """
        if len(packet) != REPORT_SIZE:
            raise ValueError(f"packet must be exactly {REPORT_SIZE} bytes, got {len(packet)}")
        self._check(packet)

        # hidapi wants the report ID as the first byte; this device uses report 0.
        self._handle.write(b"\x00" + packet)
        if not expect_reply:
            return None

        want = packet[0]
        want_vendor: tuple[int, ...] | None = None
        if packet[1] == CUSTOM_CHANNEL:
            # 0x96 channel: byte 1 is the channel, byte 2 the sub-command.
            want_vendor = (packet[1], packet[2])
        elif want in (int(Cmd.CUSTOM_GET_VALUE), int(Cmd.CUSTOM_SET_VALUE)):
            # Lighting rides on the same command with the sub-id at byte 1:
            # 0x41 is the global light info, 0x45/0x42 the per-key colours.
            # Matching on the command byte alone lets a per-key reply satisfy a
            # light-info read and vice versa.
            want_vendor = (packet[1],)

        def matches(reply: Reply) -> bool:
            if reply.command_id != want:
                return False
            if want_vendor is None:
                return True
            return tuple(reply.raw[1:1 + len(want_vendor)]) == want_vendor

        deadline = time.monotonic() + self._timeout_ms / 1000
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                break
            data = self._handle.read(REPORT_SIZE, timeout_ms=remaining)
            if not data:
                continue
            reply = Reply(bytes(data))
            if matches(reply):
                return reply
            if reply.is_unhandled:
                raise Mad68Error(
                    f"firmware reported command {want:#04x} as unhandled "
                    f"(reply {reply.raw[:4].hex(' ')})"
                )
            # Anything else is a stream report or a stale reply; drop and retry.
        detail = f"command {want:#04x}"
        if want_vendor:
            detail += f" vendor {want_vendor[1]:#04x}"
        raise Mad68Error(f"timed out waiting for reply to {detail}")

    def listen(self, seconds: float, *, only_stream: bool = False):
        """Yield inbound reports for seconds, without sending anything.

        The telemetry channel pushes reports unprompted, so it needs a read-only
        listener rather than the request/reply path. Set only_stream to yield
        just the 02 96 14 ... data-reporting reports.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                return
            data = self._handle.read(REPORT_SIZE, timeout_ms=min(remaining, 200))
            if not data:
                continue
            reply = Reply(bytes(data))
            if only_stream and not reply.is_data_reporting:
                continue
            yield reply

    def cmd(self, cmd: Cmd, payload: bytes = b"") -> Reply:
        reply = self.send(build(cmd, payload))
        assert reply is not None
        return reply

    def vendor(self, cmd: Cmd, sub: Vendor, payload: bytes = b"") -> Reply:
        reply = self.send(build_vendor(cmd, sub, payload))
        assert reply is not None
        return reply

    def vendor_get(self, sub: Vendor, payload: bytes = b"") -> Reply:
        return self.vendor(Cmd.GET_KEYBOARD_VALUE, sub, payload)

    def vendor_set(self, sub: Vendor, payload: bytes = b"") -> Reply:
        return self.vendor(Cmd.SET_KEYBOARD_VALUE, sub, payload)

    # basic queries

    def protocol_version(self) -> int:
        r = self.cmd(Cmd.GET_PROTOCOL_VERSION)
        return int.from_bytes(r.payload[:2], "big")

    def layer_count(self) -> int:
        return self.cmd(Cmd.DYNAMIC_KEYMAP_GET_LAYER_COUNT).payload[0]

    def macro_count(self) -> int:
        return self.cmd(Cmd.DYNAMIC_KEYMAP_MACRO_GET_COUNT).payload[0]

    def macro_buffer_size(self) -> int:
        r = self.cmd(Cmd.DYNAMIC_KEYMAP_MACRO_GET_BUFFER_SIZE)
        return int.from_bytes(r.payload[:2], "big")

    def keyboard_value(self, value: int) -> Reply:
        return self.cmd(Cmd.GET_KEYBOARD_VALUE, bytes([value]))

    # buffer I/O

    def read_buffer(self, cmd: Cmd, size: int, base: int = 0) -> bytes:
        """Paged VIA buffer read of size bytes starting at base."""
        out = bytearray()
        while len(out) < size:
            want = min(MAX_BUFFER_CHUNK, size - len(out))
            reply = self.send(build_buffer_read(cmd, base + len(out), want))
            assert reply is not None
            chunk = reply.buffer_data
            if not chunk:
                raise Mad68Error(
                    f"{cmd.name} at offset {base + len(out):#06x} returned no data"
                )
            out += chunk[:want]
        return bytes(out[:size])

    def write_buffer(self, cmd: Cmd, data: bytes, base: int = 0) -> None:
        """Paged VIA buffer write."""
        for off in range(0, len(data), MAX_BUFFER_CHUNK):
            chunk = data[off:off + MAX_BUFFER_CHUNK]
            self.send(build_buffer_write(cmd, base + off, chunk))

    # keymap

    def read_keymap(self) -> bytes:
        """The whole dynamic keymap: layers x rows x cols x 2 bytes.

        Sized from the connected board. A 60% board's buffer is 560 bytes, not
        the 600 of the 65% this driver was written against, and the two are not
        interchangeable: the row stride differs, so every row after the first
        would decode one column out of place.
        """
        return self.read_buffer(Cmd.DYNAMIC_KEYMAP_GET_BUFFER, self.keymap_size)

    def write_keymap(self, data: bytes) -> None:
        want = self.keymap_size
        if len(data) != want:
            raise ValueError(
                f"keymap for the {self.spec.name} must be {want} bytes "
                f"({self.spec.layers} layers x {self.spec.rows} x "
                f"{self.spec.cols} x {KEYCODE_SIZE}), got {len(data)}"
            )
        self.write_buffer(Cmd.DYNAMIC_KEYMAP_SET_BUFFER, data)

    def keymap_index(self, layer: int, row: int, col: int) -> int:
        """Byte offset of one keycode inside the flat keymap buffer."""
        if not 0 <= layer < self.spec.layers:
            raise ValueError(f"layer {layer} out of range")
        if not 0 <= row < self.matrix_rows:
            raise ValueError(f"row {row} out of range")
        if not 0 <= col < self.matrix_cols:
            raise ValueError(f"col {col} out of range")
        return keymap_offset(layer, row, col, self.matrix_rows, self.matrix_cols)

    def read_keycode(self, layer: int, row: int, col: int) -> int:
        r = self.cmd(Cmd.DYNAMIC_KEYMAP_GET_KEYCODE, bytes([layer, row, col]))
        return int.from_bytes(r.payload[3:5], "big")

    def write_keycode(self, layer: int, row: int, col: int, keycode: int) -> None:
        self.cmd(
            Cmd.DYNAMIC_KEYMAP_SET_KEYCODE,
            bytes([layer, row, col]) + keycode.to_bytes(2, "big"),
        )

    def read_macros(self) -> bytes:
        return self.read_buffer(
            Cmd.DYNAMIC_KEYMAP_MACRO_GET_BUFFER, self.macro_buffer_size()
        )

    # HE features
    #
    # DuckBread reports isV2 === true unconditionally, so the V2 packet layouts
    # are the only ones in play. V2 stores travel as uint16 big-endian in units
    # of 0.01 mm (V1 used a single byte at 0.02 mm, reading V1 offsets against
    # V2 firmware silently yields the high byte, i.e. 0 for anything < 2.56 mm).
    #
    # Vendor reply payload (payload = raw[1:]):
    #   ONE_TOG_TH: [0x96, 0x03, row, col, apc_hi, apc_lo]
    #   ONE_RT:     [0x96, 0x07, row, col, on, rel_hi, rel_lo, press_hi, press_lo]

    def read_actuation_mm(self, row: int, col: int) -> float:
        """Actuation point for one key, in millimetres."""
        p = self.vendor_get(Vendor.ONE_TOG_TH, bytes([row, col])).payload
        return travel_from_bytes(p[4:6])

    def write_actuation_mm(self, row: int, col: int, mm: float) -> None:
        self.vendor_set(Vendor.ONE_TOG_TH, bytes([row, col]) + travel_bytes(mm))

    def read_rapid_trigger(self, row: int, col: int) -> RapidTrigger:
        p = self.vendor_get(Vendor.ONE_RT, bytes([row, col])).payload
        return RapidTrigger(
            row=p[2],
            col=p[3],
            enabled=bool(p[4]),
            release_mm=travel_from_bytes(p[5:7]),
            press_mm=travel_from_bytes(p[7:9]),
        )

    def write_rapid_trigger(self, rt: RapidTrigger) -> None:
        payload = (
            bytes([rt.row, rt.col, 1 if rt.enabled else 0])
            + travel_bytes(rt.release_mm)
            + travel_bytes(rt.press_mm)
        )
        self.vendor_set(Vendor.ONE_RT, payload)

    def iter_keys(self) -> Iterator[tuple[int, int]]:
        for row in range(self.matrix_rows):
            for col in range(self.matrix_cols):
                yield row, col

    # HE features, bulk
    #
    # The bulk forms carry many keys per packet and, on write, an explicit
    # flashOp. FlashOp.NORMAL applies values without committing to flash,
    # which is what makes per-application switching free of write wear.
    #
    #   request : [cmd, 0x96, sub, _, _, off_hi, off_lo, size, ...]
    #   read    : data at byte 8
    #   write   : flashOp at byte 8, data at byte 9
    #
    # size counts keys. Offsets are linear key indices (row * cols + col).

    def key_index(self, row: int, col: int) -> int:
        return row * self.matrix_cols + col

    def key_rowcol(self, index: int) -> tuple[int, int]:
        """Inverse of key_index: linear key index back to (row, col)."""
        return divmod(index, self.matrix_cols)

    def _bulk_request(self, sub: Vendor, offset: int, count: int) -> bytes:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(sub)
        payload[BULK_OFFSET_AT - 1:BULK_OFFSET_AT + 1] = offset.to_bytes(2, "big")
        payload[BULK_SIZE_AT - 1] = count
        return build(Cmd.GET_KEYBOARD_VALUE, bytes(payload))

    def read_actuation_bulk(self, offset: int = 0, count: int | None = None) -> list[float]:
        """Actuation points in mm for count keys starting at key index offset."""
        total = self.total_keys - offset if count is None else count
        out: list[float] = []
        while len(out) < total:
            want = min(MAX_ACTUATION_READ, total - len(out))
            reply = self.send(self._bulk_request(Vendor.BUFFER_TOG_TH, offset + len(out), want))
            assert reply is not None
            data = reply.raw[BULK_READ_DATA_AT:BULK_READ_DATA_AT + want * ACTUATION_ENTRY_V2]
            if len(data) < want * ACTUATION_ENTRY_V2:
                raise Mad68Error(
                    f"bulk actuation read at {offset + len(out)} returned "
                    f"{len(data)} bytes, expected {want * ACTUATION_ENTRY_V2}"
                )
            for i in range(want):
                out.append(travel_from_bytes(data[i * 2:i * 2 + 2]))
        return out

    def read_rapid_trigger_bulk(self, offset: int = 0,
                                count: int | None = None) -> list[RapidTrigger]:
        """Rapid-trigger settings for count keys starting at key index offset."""
        total = self.total_keys - offset if count is None else count
        out: list[RapidTrigger] = []
        while len(out) < total:
            want = min(MAX_RT_READ, total - len(out))
            base = offset + len(out)
            reply = self.send(self._bulk_request(Vendor.BUFFER_RT, base, want))
            assert reply is not None
            span = want * RAPID_TRIGGER_ENTRY_V2
            data = reply.raw[BULK_READ_DATA_AT:BULK_READ_DATA_AT + span]
            if len(data) < span:
                raise Mad68Error(
                    f"bulk rapid-trigger read at {base} returned {len(data)} bytes, "
                    f"expected {span}"
                )
            for i in range(want):
                e = data[i * 5:i * 5 + 5]
                row, col = self.key_rowcol(base + i)
                out.append(
                    RapidTrigger(
                        row=row,
                        col=col,
                        enabled=bool(e[0]),
                        release_mm=travel_from_bytes(e[1:3]),
                        press_mm=travel_from_bytes(e[3:5]),
                    )
                )
        return out

    def _bulk_write(self, sub: Vendor, offset: int, count: int, data: bytes,
                    flash_op: FlashOp) -> None:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(sub)
        payload[BULK_OFFSET_AT - 1:BULK_OFFSET_AT + 1] = offset.to_bytes(2, "big")
        payload[BULK_SIZE_AT - 1] = count
        payload[BULK_FLASH_OP_AT - 1] = int(flash_op)
        start = BULK_WRITE_DATA_AT - 1
        payload[start:start + len(data)] = data
        self.send(build(Cmd.SET_KEYBOARD_VALUE, bytes(payload)))

    def write_actuation_bulk(self, values: list[float], offset: int = 0, *,
                             flash_op: FlashOp = FlashOp.NORMAL) -> int:
        """Write actuation points in mm. Returns the number of packets sent."""
        packets = 0
        i = 0
        while i < len(values):
            chunk = values[i:i + MAX_ACTUATION_WRITE]
            data = b"".join(
                travel_bytes(v) for v in chunk
            )
            self._bulk_write(Vendor.BUFFER_TOG_TH, offset + i, len(chunk), data, flash_op)
            packets += 1
            i += len(chunk)
        return packets

    def write_rapid_trigger_bulk(self, specs: list[RapidTrigger], offset: int = 0, *,
                                 flash_op: FlashOp = FlashOp.NORMAL) -> int:
        """Write rapid-trigger settings. Returns the number of packets sent."""
        packets = 0
        i = 0
        while i < len(specs):
            chunk = specs[i:i + MAX_RT_WRITE]
            data = bytearray()
            for rt in chunk:
                data.append(1 if rt.enabled else 0)
                data += travel_bytes(rt.release_mm)
                data += travel_bytes(rt.press_mm)
            self._bulk_write(Vendor.BUFFER_RT, offset + i, len(chunk), bytes(data), flash_op)
            packets += 1
            i += len(chunk)
        return packets

    # telemetry
    #
    # realTimeAdcAxleBuffer (0x16) and realTimeTripAxleBuffer (0x17) use the
    # same offset/size bulk header as the actuation buffers, with 2 bytes per key.
    # Confirmed against hardware: 0x16 returns raw sensor values around 2990-3085
    # at rest; 0x17 reads 0 for a key that is not pressed.
    #
    # These sub-commands are named in the web bundle's vendor enum but never used
    # by any of its code, so the layout here comes from probing the firmware
    # (tools/telemetry_probe.py), not from a reference implementation.

    _TELEMETRY_ENTRY = 2
    MAX_TELEMETRY_READ = (REPORT_SIZE - BULK_READ_DATA_AT) // _TELEMETRY_ENTRY  # 12

    def _read_u16_bulk(self, sub: Vendor, count: int, offset: int = 0) -> list[int]:
        out: list[int] = []
        while len(out) < count:
            want = min(self.MAX_TELEMETRY_READ, count - len(out))
            # Telemetry reads are idempotent, and the firmware occasionally drops
            # one when it is busy. Retry rather than failing a whole sweep.
            for attempt in range(3):
                try:
                    reply = self.send(self._bulk_request(sub, offset + len(out), want))
                    break
                except Mad68Error:
                    if attempt == 2:
                        raise
            assert reply is not None
            span = want * self._TELEMETRY_ENTRY
            data = reply.raw[BULK_READ_DATA_AT:BULK_READ_DATA_AT + span]
            if len(data) < span:
                raise Mad68Error(
                    f"{sub.name} at {offset + len(out)} returned {len(data)} bytes, "
                    f"expected {span}"
                )
            for i in range(want):
                out.append(int.from_bytes(data[i * 2:i * 2 + 2], "big"))
        return out

    def read_adc_raw(self, count: int | None = None) -> list[int]:
        """Raw per-key sensor values. Higher/lower is switch dependent."""
        return self._read_u16_bulk(
            Vendor.REALTIME_ADC_AXLE_BUFFER, self.total_keys if count is None else count
        )

    def read_trip_raw(self, count: int | None = None) -> list[int]:
        """Per-key trip/travel values; 0 for a key that is not pressed."""
        return self._read_u16_bulk(
            Vendor.REALTIME_TRIP_AXLE_BUFFER, self.total_keys if count is None else count
        )

    def read_trip_mm(self, row: int, col: int) -> float:
        """Live travel for one key, in millimetres.

        One packet, so it can be polled fast enough to drive a travel gauge, a
        full-matrix sweep caps out around 20 Hz, which visibly steps.
        """
        p = self.vendor_get(Vendor.REALTIME_TRIP_AXLE, bytes([row, col])).payload
        return round(int.from_bytes(p[4:6], "big") * 0.01, 2)

    def read_adc_key(self, row: int, col: int) -> int:
        """Raw sensor value for one key (u16be at payload byte 4)."""
        p = self.vendor_get(Vendor.REALTIME_ADC_AXLE, bytes([row, col])).payload
        return int.from_bytes(p[4:6], "big")

    def populated_mask(self) -> list[bool]:
        """Which matrix positions actually have a switch, cached per handle.

        No board in this family fills its matrix: a 68-key board leaves 7 of its
        75 positions empty, a 61-key 60% board leaves 9 of its 70. Those have no
        sensor and no LED: their calibration flag is clear and their colour
        always reads back black no matter what is written. Anything diffing
        per-key state has to skip them or it will never converge.

        This is also the only authority on which physical key sits where, so
        the UI's drawn layout is checked against it rather than assumed.
        """
        if getattr(self, "_populated", None) is None:
            try:
                self._populated = [bool(v) for v in self.read_calibration_status()]
            except Exception:
                self._populated = [True] * self.total_keys
        return self._populated

    def read_calibration_status(self, count: int | None = None) -> list[int]:
        """Per-key calibration flag; 1 means calibrated. Read-only."""
        total = self.total_keys if count is None else count
        out: list[int] = []
        per_packet = REPORT_SIZE - BULK_READ_DATA_AT
        while len(out) < total:
            want = min(per_packet, total - len(out))
            reply = self.send(self._bulk_request(Vendor.CALIBRATION, len(out), want))
            assert reply is not None
            out.extend(reply.raw[BULK_READ_DATA_AT:BULK_READ_DATA_AT + want])
        return out[:total]

    # dynamic keystroke (DKS, vendor 0x0F)
    #
    #   [cmd, 0x96, 0x0F, _, _, subField, index, ...4 actions x 4 bytes]
    #        raw:  1     2  3  4    5       6      7 ..
    # subField is SubField.GET / SubField.SET.

    def read_dks(self, index: int) -> Dks:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(Vendor.DKS)
        payload[4] = int(SubField.GET)
        payload[5] = index
        reply = self.send(build(Cmd.GET_KEYBOARD_VALUE, bytes(payload)))
        assert reply is not None
        body = reply.raw[7:7 + DKS_ACTION_COUNT * DKS_ACTION_SIZE]
        return Dks.from_bytes(index, body)

    def write_dks(self, dks: Dks) -> None:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(Vendor.DKS)
        payload[4] = int(SubField.SET)
        payload[5] = dks.index
        body = dks.to_bytes()
        payload[6:6 + len(body)] = body
        self.send(build(Cmd.SET_KEYBOARD_VALUE, bytes(payload)))

    # hold/click, a.k.a. tap dance (MT)
    #
    # Different framing again: this one rides on VIA's vial prefix, not the
    # 0x96 channel.
    #   [0xFE, 0x0D(entryOp), 0x01 get / 0x02 set, index,
    #    tap_hi, tap_lo, hold_hi, hold_lo, timer_u16be]

    def read_tap_dance(self, index: int) -> TapDance:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = int(Vendor.ENTRY_OP)
        payload[1] = int(Vendor.TAP_DANCE_GET)
        payload[2] = index
        r = self.send(build(Cmd.VIAL_PREFIX, bytes(payload)))
        assert r is not None
        raw = r.raw
        return TapDance(
            index=raw[3],
            tap=(raw[4] << 8) | raw[5],
            hold=(raw[6] << 8) | raw[7],
            timer_ms=int.from_bytes(raw[8:10], "big"),
        )

    def write_tap_dance(self, td: TapDance) -> None:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = int(Vendor.ENTRY_OP)
        payload[1] = int(Vendor.TAP_DANCE_SET)
        payload[2] = td.index
        payload[3] = (td.tap >> 8) & 0xFF
        payload[4] = td.tap & 0xFF
        payload[5] = (td.hold >> 8) & 0xFF
        payload[6] = td.hold & 0xFF
        payload[7:9] = max(0, min(0xFFFF, td.timer_ms)).to_bytes(2, "big")
        self.send(build(Cmd.VIAL_PREFIX, bytes(payload)))

    # advanced keys: rapid snap / SOCD / OKS (vendor 0x20)

    def read_advanced_key(self, index: int) -> AdvancedKey:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(Vendor.RS)
        payload[4] = int(SubField.GET)
        payload[5] = index
        payload[6] = index  # the app sets id to the index on read
        r = self.send(build(Cmd.GET_KEYBOARD_VALUE, bytes(payload)))
        assert r is not None
        raw = r.raw
        try:
            mode = AdvancedKeyMode(raw[7])
        except ValueError:
            mode = AdvancedKeyMode.NONE
        return AdvancedKey(
            index=raw[6], mode=mode, id=raw[8],
            rs_apc_lv=int.from_bytes(raw[9:11], "big"),
            gapc_sw=raw[11], rt_sw=raw[12],
            key1_row=raw[13], key1_col=raw[14],
            key2_row=raw[15], key2_col=raw[16],
            layer=raw[17],
        )

    def write_advanced_key(self, key: AdvancedKey) -> None:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = CUSTOM_CHANNEL
        payload[1] = int(Vendor.RS)
        payload[2] = 0
        payload[3] = 0
        payload[4] = int(SubField.SET)
        payload[5] = key.index
        payload[6] = int(key.mode)
        # The app folds ids >= 0x1a down by 0x1a before sending.
        payload[7] = key.id - 0x1A if key.id >= 0x1A else key.id
        payload[8:10] = max(0, min(0xFFFF, key.rs_apc_lv)).to_bytes(2, "big")
        payload[10] = key.gapc_sw
        payload[11] = key.rt_sw
        payload[12] = key.key1_row
        payload[13] = key.key1_col
        payload[14] = key.key2_row
        payload[15] = key.key2_col
        payload[16] = key.layer
        self.send(build(Cmd.SET_KEYBOARD_VALUE, bytes(payload)))

    # per-key RGB (VIA custom value framing, no 0x96 byte)
    #
    #   read  : [0x08, 0x45, row, col, num]
    #   write : [0x07, 0x42, row, col, num, r,g,b, ...]
    #   save  : [0x03, 0x96, 0x12]

    # RGB triples that fit in one packet: (32 - 5) // 3.
    MAX_RGB_PER_PACKET = (REPORT_SIZE - 5) // 3

    def read_key_colors(self, row: int, col: int, count: int = 1) -> list[tuple[int, int, int]]:
        count = min(count, self.MAX_RGB_PER_PACKET)
        r = self.send(build(Cmd.CUSTOM_GET_VALUE,
                            bytes([int(Vendor.GET_CUSTOM_LAMPLIGHT), row, col, count])))
        assert r is not None
        out = []
        for i in range(count):
            base = 5 + i * 3
            out.append(decode_rgb(r.raw[base:base + 3]))
        return out

    def write_key_colors(self, row: int, col: int,
                         colors: list[tuple[int, int, int]]) -> None:
        if len(colors) > self.MAX_RGB_PER_PACKET:
            raise ValueError(
                f"at most {self.MAX_RGB_PER_PACKET} colours per packet, got {len(colors)}"
            )
        body = bytearray([int(Vendor.SET_CUSTOM_LAMPLIGHT), row, col, len(colors)])
        for r_, g_, b_ in colors:
            body += encode_rgb(r_, g_, b_)
        self.send(build(Cmd.CUSTOM_SET_VALUE, bytes(body)))

    def read_all_key_colors(self) -> list[tuple[int, int, int]]:
        """Per-key RGB for every matrix position, in linear key order."""
        out: list[tuple[int, int, int]] = []
        while len(out) < self.total_keys:
            idx = len(out)
            want = min(self.MAX_RGB_PER_PACKET, self.total_keys - idx)
            row, col = self.key_rowcol(idx)
            out.extend(self.read_key_colors(row, col, want))
        return out[:self.total_keys]

    def write_all_key_colors(self, colors: list[tuple[int, int, int]]) -> int:
        packets = 0
        i = 0
        while i < min(len(colors), self.total_keys):
            chunk = colors[i:i + self.MAX_RGB_PER_PACKET]
            row, col = self.key_rowcol(i)
            self.write_key_colors(row, col, chunk)
            packets += 1
            i += len(chunk)
        return packets

    def save_lighting(self) -> None:
        """Commit lighting to flash. Deliberate save only."""
        self.vendor_set(Vendor.SAVE_LAMPLIGHT)

    # global lighting (id_lighting_get/set_value, sub at byte 1)

    def read_light_info(self) -> LightInfo:
        r = self.send(build(Cmd.CUSTOM_GET_VALUE, bytes([int(Vendor.LIGHT_INFO)])))
        assert r is not None
        red, green, blue = decode_rgb(r.raw[5:8])
        return LightInfo(effect=r.raw[2], speed=r.raw[4],
                         r=red, g=green, b=blue, brightness=r.raw[8])

    def write_light_info(self, info: LightInfo) -> None:
        payload = bytearray(MAX_PAYLOAD)
        payload[0] = int(Vendor.LIGHT_INFO)
        payload[1] = info.effect
        payload[3] = info.speed
        payload[4:7] = encode_rgb(info.r, info.g, info.b)
        # Above BRIGHTNESS_MAX the firmware drops this field and keeps whatever
        # was there before, while still applying the effect and colour from the
        # same packet, so an unclamped write reports success and changes
        # nothing. Clamp rather than let that happen silently.
        payload[7] = min(info.brightness, BRIGHTNESS_MAX)
        self.send(build(Cmd.CUSTOM_SET_VALUE, bytes(payload)))

    # vendor scalars
    #
    # All follow [cmd, 0x96, sub, value0, value1, ...] with values from byte 3.

    def _scalar_get(self, sub: Vendor) -> bytes:
        return self.vendor_get(sub).payload[2:]

    def _scalar_set(self, sub: Vendor, values: bytes) -> None:
        self.vendor_set(sub, values)

    def read_dead_band(self) -> DeadBand:
        d = self._scalar_get(Vendor.DEAD_BAND)
        return DeadBand(top_mm=int.from_bytes(d[0:2], "big") * 0.01,
                        bottom_mm=int.from_bytes(d[2:4], "big") * 0.01)

    def write_dead_band(self, band: DeadBand) -> None:
        self._scalar_set(
            Vendor.DEAD_BAND,
            max(0, min(0xFFFF, round(band.top_mm * 100))).to_bytes(2, "big")
            + max(0, min(0xFFFF, round(band.bottom_mm * 100))).to_bytes(2, "big"),
        )

    def read_feature(self) -> KeyboardFeature:
        d = self._scalar_get(Vendor.FEATURE)
        return KeyboardFeature(rgb_area=d[0], wasd_switch=d[1], mac_switch=d[2],
                               win_lock=d[3], nkro_switch=d[4])

    def write_feature(self, f: KeyboardFeature) -> None:
        self._scalar_set(Vendor.FEATURE, bytes([f.rgb_area, f.wasd_switch,
                                                f.mac_switch, f.win_lock,
                                                f.nkro_switch]))

    def read_game_mode(self) -> int:
        return self._scalar_get(Vendor.GAME_MODE)[0]

    def write_game_mode(self, mode: int) -> None:
        self._scalar_set(Vendor.GAME_MODE, bytes([mode & 0xFF]))

    def read_bottom_optimize(self) -> int:
        return self._scalar_get(Vendor.BOTTOM_OPTIMIZE_SWITCH)[0]

    def write_bottom_optimize(self, opt: int) -> None:
        self._scalar_set(Vendor.BOTTOM_OPTIMIZE_SWITCH, bytes([opt & 0xFF]))

    def read_box_light(self) -> BoxLight:
        d = self._scalar_get(Vendor.BOX_LIGHT)
        return BoxLight(mode=d[0], colorful=d[1], brightness=d[2], speed=d[3])

    def write_box_light(self, box: BoxLight) -> None:
        self._scalar_set(Vendor.BOX_LIGHT,
                         bytes([box.mode, box.colorful, box.brightness, box.speed]))

    def read_layer_setting(self) -> int:
        return self._scalar_get(Vendor.LAYER)[0]

    # macros

    def read_macro_steps(self) -> list[list[MacroStep]]:
        """Every macro, decoded into steps."""
        buffer = self.read_macros()
        return [decode_macro(m) for m in split_macro_buffer(buffer, self.macro_count())]

    def write_macro_steps(self, macros: list[list[MacroStep]], *,
                          preserve_tail: bool = True) -> None:
        """Encode and write every macro into the shared buffer.

        The buffer holds macro_count() NUL-terminated macros followed by unused
        space, but "unused" is not necessarily "empty". On this keyboard the
        region past the terminators held 204 bytes of live-looking data while all
        16 slots parsed as empty. Rebuilding the buffer purely from the parsed
        macros therefore destroys whatever is out there, which is a silent and
        surprising side effect of editing one macro.

        So by default the tail beyond the managed macros is read back and carried
        over unchanged. Pass preserve_tail=False for a deliberate full wipe.
        """
        size = self.macro_buffer_size()
        encoded = [encode_macro(steps) for steps in macros]

        body = bytearray()
        for m in encoded:
            body += m
            body.append(0x00)
        if len(body) > size:
            raise ValueError(f"macros need {len(body)} bytes, buffer is {size}")

        tail = bytes(size - len(body))
        if preserve_tail:
            current = self.read_macros()
            # Skip past the same number of terminators in the existing buffer to
            # find where our managed region ends there.
            pos, seen = 0, 0
            while seen < len(encoded) and pos < len(current):
                nxt = current.find(b"\x00", pos)
                if nxt == -1:
                    pos = len(current)
                    break
                pos = nxt + 1
                seen += 1
            keep = current[pos:]
            tail = (keep + bytes(size))[:size - len(body)]

        self.write_buffer(Cmd.DYNAMIC_KEYMAP_MACRO_SET_BUFFER, bytes(body) + tail)

    def write_macro_slot(self, index: int, steps: list[MacroStep]) -> None:
        """Replace one macro, leaving the others, and the tail, untouched."""
        count = self.macro_count()
        if not 0 <= index < count:
            raise ValueError(f"macro index {index} out of range 0..{count - 1}")
        macros = self.read_macro_steps()
        macros[index] = steps
        self.write_macro_steps(macros)

    def commit_to_flash(self) -> None:
        """Persist the currently applied HE values across a power cycle.

        Re-sends the live values with ERASE_AND_WRITE. Call this only for a
        deliberate save, never on an automatic profile switch.
        """
        actuation = self.read_actuation_bulk()
        triggers = self.read_rapid_trigger_bulk()
        self.write_actuation_bulk(actuation, flash_op=FlashOp.ERASE_AND_WRITE)
        self.write_rapid_trigger_bulk(triggers, flash_op=FlashOp.ERASE_AND_WRITE)

    def iter_probe_reads(self) -> Iterator[tuple[str, bytes]]:
        """Read-only sweep of the informational vendor sub-commands."""
        safe = [
            Vendor.DEAD_BAND,
            Vendor.FEATURE,
            Vendor.GAME_MODE,
            Vendor.BOTTOM_OPTIMIZE_SWITCH,
            Vendor.LIGHT_INFO,
            Vendor.LAYER,
            Vendor.RS,
            Vendor.BOX_LIGHT,
            Vendor.MIX_AXLE,
        ]
        for sub in safe:
            try:
                yield sub.name, self.vendor_get(sub).payload
            except Mad68Error as exc:
                yield sub.name, f"ERROR: {exc}".encode()
