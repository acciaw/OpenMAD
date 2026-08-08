"""Every keyboard on the DuckBread controller, and what makes each one differ.

The driver was written against one board, the MAD68 HE, but the protocol is not
that board's -- it is the controller's. The vendor's web configurator pairs each
keyboard with a controller class and a small per-board config object, and 25 of
its entries name the same `DuckBread` controller this driver reverse engineered.
The packet layouts, the vendor sub-commands and the flash semantics are shared
by all of them; only the matrix geometry changes.

That is what this table is. It came out of the vendor's own registry (see
research/notes/protocol.md), not from guesswork:

    { controller: DuckBread, digital: Qc, filters: [ { vendorId: 0x373b,
      productId: 0x1058, custom: { layer: 4, row: 5, col: 15, ... } } ] }

Two things are worth noticing in the numbers below. Every board is five rows and
four layers without exception, so only the column count varies -- 14 for the 60%
boards, 15 for the 65%/68 boards, and 6 for the one macropad. And a board being
listed here means the vendor ships it on this controller, not that anyone has
run this driver against it. `verified` is the honest record of which ones have
actually been exercised on hardware.
"""

from __future__ import annotations

from dataclasses import dataclass

VENDOR_ID = 0x373B


@dataclass(frozen=True)
class BoardSpec:
    """One keyboard: how to find it, and how big its matrix is."""

    product_id: int
    name: str
    rows: int
    cols: int
    layers: int
    bootloader_id: int
    # True only for hardware this driver has actually been run against. Anything
    # false is supported on the strength of a shared controller, which is a good
    # bet but not a tested one -- the UI says so, and writes stay gated until the
    # board's own protocol version confirms it.
    verified: bool = False

    @property
    def total_keys(self) -> int:
        return self.rows * self.cols

    @property
    def form_factor(self) -> str:
        """Rough physical class, used to pick which layout the HUD draws."""
        if self.cols <= 8:
            return "macropad"
        return "60" if self.cols <= 14 else "65"


# Ordered roughly as the vendor lists them. Names come from the config classes
# in its bundle (_DuckBreadMad68HERgb and friends), so they match what the
# official tool calls each board.
BOARDS: tuple[BoardSpec, ...] = (
    # -- 60% family, 5x14 --------------------------------------------------
    BoardSpec(0x1053, "MAD60 HE",          5, 14, 4, 0x201F),
    BoardSpec(0x1054, "MAD60 HE RGB",      5, 14, 4, 0x2020),
    BoardSpec(0x1055, "MAD60 HE Ultra",    5, 14, 4, 0x2021),
    BoardSpec(0x1056, "MAD60 HE Limited",  5, 14, 4, 0x2022),
    BoardSpec(0x10AD, "MAD60 Pro",         5, 14, 4, 0x2053),
    BoardSpec(0x1120, "MAD60 HE V2",       5, 14, 4, 0x210B),
    BoardSpec(0x10F6, "MAD Light 60 HE",   5, 14, 4, 0x2085),
    BoardSpec(0x110B, "MAD63 HE",          5, 14, 4, 0x2102),
    BoardSpec(0x112A, "Z60",               5, 14, 4, 0x2115),

    # -- 65% / 68 family, 5x15 ---------------------------------------------
    # 0x1057 carries no name in the vendor bundle. It sits immediately before
    # the RGB model with identical geometry, so it is almost certainly the base
    # MAD68 HE; labelled as such but left unverified like the rest.
    BoardSpec(0x1057, "MAD68 HE",          5, 15, 4, 0x2023),
    BoardSpec(0x1058, "MAD68 HE RGB",      5, 15, 4, 0x2024, verified=True),
    BoardSpec(0x1059, "MAD68 HE Ultra",    5, 15, 4, 0x2025),
    BoardSpec(0x105A, "MAD68 HE Limited",  5, 15, 4, 0x2026),
    BoardSpec(0x10A7, "MAD68 R",           5, 15, 4, 0x204E),
    BoardSpec(0x10D6, "MAD68 Pro",         5, 15, 4, 0x2075),
    BoardSpec(0x1123, "MAD68 HE V2",       5, 15, 4, 0x210E),
    BoardSpec(0x1144, "MAD Light 68 HE",   5, 15, 4, 0x212D),
    BoardSpec(0x1136, "Z68",               5, 15, 4, 0x2121),
    BoardSpec(0x1139, "MAG68",             5, 15, 4, 0x2124),
    BoardSpec(0x10A3, "FIRE68",            5, 15, 4, 0x204A),
    BoardSpec(0x10A9, "FIRE68",            5, 15, 4, 0x2050),
    BoardSpec(0x10AA, "FIRE68 Pro",        5, 15, 4, 0x2051),
    BoardSpec(0x10A4, "FIRE68 Ultra",      5, 15, 4, 0x204B),
    BoardSpec(0x10AB, "FIRE68 Ultra",      5, 15, 4, 0x2052),

    # -- macropad, 5x6 -----------------------------------------------------
    BoardSpec(0x107D, "MAD Smart",         5,  6, 4, 0x2035),
)

BY_PRODUCT_ID: dict[int, BoardSpec] = {b.product_id: b for b in BOARDS}
BOOTLOADER_IDS: frozenset[int] = frozenset(b.bootloader_id for b in BOARDS)

# The board this driver was developed and confirmed against. Used as the
# fallback geometry when nothing is connected and something still has to render.
DEFAULT_BOARD: BoardSpec = BY_PRODUCT_ID[0x1058]


# Boards the user has explicitly unlocked for writing despite unverified
# firmware.
#
# Kept here rather than in settings.py so the transport can consult it without
# depending on the app's settings or path modules -- device.py has no business
# knowing where the data directory is. Whoever owns the data directory calls
# load_unlocked() once at startup.
_UNLOCKED: set[int] = set()


def is_unlocked(product_id: int) -> bool:
    return product_id in _UNLOCKED


def unlock(product_id: int, on: bool = True) -> None:
    if on:
        _UNLOCKED.add(product_id)
    else:
        _UNLOCKED.discard(product_id)


def unlocked_ids() -> list[int]:
    return sorted(_UNLOCKED)


def load_unlocked(path) -> None:
    """Read the unlock list. A missing or unreadable file means nothing is."""
    import json
    from pathlib import Path
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        _UNLOCKED.clear()
        _UNLOCKED.update(int(x, 0) if isinstance(x, str) else int(x)
                         for x in raw.get("unlocked", []))
    except Exception:
        _UNLOCKED.clear()


def save_unlocked(path) -> None:
    import json
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"unlocked": [f"{pid:#06x}" for pid in sorted(_UNLOCKED)]}, indent=2),
        encoding="utf-8")


def lookup(product_id: int) -> BoardSpec | None:
    """The spec for a product ID, or None if it is not a DuckBread board."""
    return BY_PRODUCT_ID.get(product_id)


def describe(product_id: int) -> BoardSpec:
    """Like lookup(), but never None.

    An unrecognised product ID on the vendor's VID that answers on the config
    interface is still worth talking to -- it is far more likely to be a newer
    board than something hostile. It gets the common geometry and is reported as
    unverified, which keeps writes gated until its protocol version says
    otherwise.
    """
    spec = BY_PRODUCT_ID.get(product_id)
    if spec is not None:
        return spec
    return BoardSpec(
        product_id=product_id,
        name=f"Unrecognised board ({product_id:#06x})",
        rows=DEFAULT_BOARD.rows,
        cols=DEFAULT_BOARD.cols,
        layers=DEFAULT_BOARD.layers,
        bootloader_id=0,
        verified=False,
    )
