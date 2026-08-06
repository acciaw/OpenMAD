"""Local driver for the MAD68 HE magnetic-switch keyboard (VIA protocol)."""

from .device import (
    DeviceNotFound,
    Mad68,
    Mad68Error,
    WriteBlocked,
    find_config_interface,
    find_interfaces,
    in_bootloader,
)
from .protocol import (
    VOLATILE_KEYBOARD_VALUES,
    Cmd,
    KeyboardValue,
    Reply,
    Vendor,
    build,
    build_vendor,
    mm_to_travel,
    travel_to_mm,
)

__all__ = [
    "VOLATILE_KEYBOARD_VALUES",
    "Cmd",
    "DeviceNotFound",
    "KeyboardValue",
    "Mad68",
    "Mad68Error",
    "Reply",
    "Vendor",
    "WriteBlocked",
    "build",
    "build_vendor",
    "find_config_interface",
    "find_interfaces",
    "in_bootloader",
    "mm_to_travel",
    "travel_to_mm",
]
