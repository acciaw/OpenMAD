"""QMK keycode names, enough to label a keymap readably.

Not exhaustive, it covers the basic HID usage range plus the QMK special ranges
this keyboard actually uses (layer keys, modifiers). Anything unrecognised is
rendered as hex rather than guessed at.
"""

from __future__ import annotations

from .protocol import MATRIX_COLS, MATRIX_ROWS

# Basic HID keyboard usages and the QMK names for them.
BASIC: dict[int, str] = {
    0x00: "----",   # KC_NO
    0x01: "XXXX",   # KC_TRNS / transparent
    0x28: "Enter",
    0x29: "Esc",
    0x2A: "Bspc",
    0x2B: "Tab",
    0x2C: "Space",
    0x2D: "-",
    0x2E: "=",
    0x2F: "[",
    0x30: "]",
    0x31: "\\",
    0x32: "#",
    0x33: ";",
    0x34: "'",
    0x35: "`",
    0x36: ",",
    0x37: ".",
    0x38: "/",
    0x39: "Caps",
    0x46: "PrtSc",
    0x47: "ScrLk",
    0x48: "Pause",
    0x49: "Ins",
    0x4A: "Home",
    0x4B: "PgUp",
    0x4C: "Del",
    0x4D: "End",
    0x4E: "PgDn",
    0x4F: "Rgt",
    0x50: "Lft",
    0x51: "Dn",
    0x52: "Up",
    0x65: "App",
    0xE0: "LCtl",
    0xE1: "LSft",
    0xE2: "LAlt",
    0xE3: "LGui",
    0xE4: "RCtl",
    0xE5: "RSft",
    0xE6: "RAlt",
    0xE7: "RGui",
}
for _i in range(26):
    BASIC[0x04 + _i] = chr(ord("A") + _i)
for _i, _ch in enumerate("1234567890"):
    BASIC[0x1E + _i] = _ch
for _i in range(12):
    BASIC[0x3A + _i] = f"F{_i + 1}"
for _i in range(12):
    BASIC[0x68 + _i] = f"F{_i + 13}"

# QMK 16-bit layer ranges, modern (post-keycode-rework) layout, 0x20 apart.
# Confirmed against this device: the Fn key at row 4 col 10 reads 0x5221, which
# is MO(1) under this scheme. That also matches Tu.layer = 0x52 in the bundle.
_LAYER_RANGES: tuple[tuple[int, str], ...] = (
    (0x5200, "TO({})"),
    (0x5220, "MO({})"),
    (0x5240, "DF({})"),
    (0x5260, "TG({})"),
    (0x5280, "OSL({})"),
)
_LAYER_SPAN = 0x1F


def label(keycode: int) -> str:
    """Short human-readable name for a 16-bit QMK keycode."""
    if keycode <= 0xFF:
        return BASIC.get(keycode, f"{keycode:#04x}")
    for base, fmt in _LAYER_RANGES:
        if base <= keycode <= base + _LAYER_SPAN:
            return fmt.format(keycode - base)
    return f"{keycode:#06x}"


def decode_keymap(keymap: bytes, layer: int, rows: int = MATRIX_ROWS,
                  cols: int = MATRIX_COLS) -> list[list[int]]:
    """Split a raw keymap buffer into rows of 16-bit keycodes for one layer.

    The row stride is the board's own column count, so callers holding a device
    must pass kb.matrix_rows / kb.matrix_cols. Reading a 14-column board's
    buffer with a stride of 15 shifts every row after the first.
    """
    out = []
    for row in range(rows):
        cells = []
        for col in range(cols):
            off = ((layer * rows + row) * cols + col) * 2
            cells.append(int.from_bytes(keymap[off:off + 2], "big"))
        out.append(cells)
    return out


def render_matrix(keymap: bytes, layer: int = 0, *,
                  annotate: dict[tuple[int, int], str] | None = None,
                  width: int = 8, rows: int = MATRIX_ROWS,
                  cols: int = MATRIX_COLS) -> str:
    """A text grid of one layer, optionally annotating specific positions."""
    grid = decode_keymap(keymap, layer, rows, cols)
    lines = ["      " + "".join(f"{c:>{width}}" for c in range(cols))]
    for r, cells in enumerate(grid):
        rendered = []
        for c, kc in enumerate(cells):
            text = label(kc)
            if annotate and (r, c) in annotate:
                text = f"{text}*"
            rendered.append(f"{text:>{width}}")
        lines.append(f"  r{r}  " + "".join(rendered))
    return "\n".join(lines)
