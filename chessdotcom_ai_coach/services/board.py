"""Server-side chessboard rendering helpers.

Ports the logic from the "Gambit" design's Board component (Board.dc.html) to
Python: the Django template language can't parse a FEN, so we expand it into a
flat list of 64 cells the template can iterate over. Also exposes a couple of
cheap position helpers (move number, side to move, last move) used to enrich the
game views.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# Unicode glyphs, keyed by lowercase piece letter. Colour is conveyed by the
# `white` flag on each cell (styled via CSS), not by separate glyphs.
_GLYPHS = {
    "k": "♚",  # ♚
    "q": "♛",  # ♛
    "r": "♜",  # ♜
    "b": "♝",  # ♝
    "n": "♞",  # ♞
    "p": "♟",  # ♟
}

_FILES = "abcdefgh"
_STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


def fen_to_cells(
    fen: Optional[str],
    highlight: Optional[List[str]] = None,
    flipped: bool = False,
) -> List[Dict]:
    """Expand a FEN into 64 cells for the board template.

    Each cell is a dict: ``{glyph, light, highlight, white}``. ``highlight`` is a
    list of square names (e.g. ``["d4", "f5"]``) to ring. Returns cells in
    reading order (rank 8 → 1, file a → h), or reversed when ``flipped``.
    """
    board_part = (fen or _STARTING_FEN).split(" ")[0]
    rows = board_part.split("/")
    if len(rows) != 8:  # malformed FEN — fall back to the empty board
        rows = ["8"] * 8

    highlight_set = {sq.lower() for sq in (highlight or [])}
    cells: List[Dict] = []

    for r in range(8):
        rank = 8 - r
        parsed: List[Optional[str]] = []
        for ch in rows[r]:
            if ch.isdigit():
                parsed.extend([None] * int(ch))
            else:
                parsed.append(ch)
        # Pad/truncate defensively so every rank has 8 files.
        parsed = (parsed + [None] * 8)[:8]

        for f in range(8):
            piece = parsed[f]
            name = _FILES[f] + str(rank)
            is_light = (r + f) % 2 == 0
            cells.append(
                {
                    "glyph": _GLYPHS[piece.lower()] if piece else "",
                    "light": is_light,
                    "highlight": name in highlight_set,
                    "white": piece.isupper() if piece else False,
                }
            )

    if flipped:
        cells.reverse()
    return cells


def active_color(fen: Optional[str]) -> str:
    """Return ``"white"`` or ``"black"`` — the side to move, from the FEN."""
    parts = (fen or "").split(" ")
    if len(parts) >= 2 and parts[1] in ("w", "b"):
        return "white" if parts[1] == "w" else "black"
    return "white"


def fullmove_number(fen: Optional[str]) -> Optional[int]:
    """Return the full-move number (FEN field 6), or ``None`` if absent."""
    parts = (fen or "").split(" ")
    if len(parts) >= 6:
        try:
            return int(parts[5])
        except ValueError:
            return None
    return None


def last_move_from_pgn(pgn: Optional[str]) -> Optional[str]:
    """Extract the last move played from a PGN, with its move number.

    Returns something like ``"11...Be7"`` or ``"12. Nf5"``, or ``None`` when the
    movetext can't be parsed.
    """
    if not pgn:
        return None

    # Drop the header tags; keep the movetext that follows the blank line.
    movetext = pgn
    if "\n\n" in pgn:
        movetext = pgn.split("\n\n", 1)[1]

    # Strip comments, NAGs, result markers and variation parens.
    movetext = re.sub(r"\{[^}]*\}", " ", movetext)
    movetext = re.sub(r"\$\d+", " ", movetext)
    movetext = re.sub(r"[()]", " ", movetext)
    movetext = re.sub(r"\b(1-0|0-1|1/2-1/2|\*)\b", " ", movetext)

    # Tokens: a move number like "12." / "12..." or a SAN move.
    tokens = movetext.split()
    last_san = None
    last_number = None
    pending_black = False  # True once we've seen "N..." (black to move)
    for tok in tokens:
        m = re.fullmatch(r"(\d+)\.(\.\.)?", tok)
        if m:
            last_number = int(m.group(1))
            pending_black = bool(m.group(2))
            continue
        # A SAN move (allow check/mate/promotion/castle marks).
        if re.fullmatch(r"[NBRQKOa-h][A-Za-z0-9+#=\-]*", tok):
            last_san = tok
            if last_number is not None:
                sep = "..." if pending_black else ". "
                last_san_labeled = f"{last_number}{sep}{tok}"
            else:
                last_san_labeled = tok
            # After a white move, the next SAN (same number) is black's.
            pending_black = True
    return last_san_labeled if last_san else None
