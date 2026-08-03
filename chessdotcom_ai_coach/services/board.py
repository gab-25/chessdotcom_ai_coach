"""Server-side chessboard rendering helpers.

Ports the logic from the "Gambit" design's Board component (Board.dc.html) to
Python: the Django template language can't parse a FEN, so we expand it into a
flat list of 64 cells the template can iterate over. Also exposes a couple of
cheap position helpers (move number, side to move, last move) used to enrich the
game views.
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Dict, List, Optional

import chess
import chess.pgn

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


_LAST_PLY_KEYS = ("move_no", "color", "san", "uci", "fen_after")


@lru_cache(maxsize=512)
def _last_ply_tuple(pgn: str) -> Optional[tuple]:
    """Replay ``pgn`` once and return its last ply plus the reached FEN, as a tuple.

    Split out from :func:`last_ply_from_pgn` purely so the memoisation can hang off
    an immutable value: a cached dict would be handed to callers free to mutate it,
    poisoning every later cache hit. Keyed on the PGN text, which only changes when
    a move is actually played — so the home page's 5s poll re-replays a game only
    on the tick where it advanced.
    """
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        return None
    if game is None:
        return None

    board = game.board()
    last: Optional[tuple] = None
    for move in game.mainline_moves():
        move_no = board.fullmove_number
        color = "white" if board.turn == chess.WHITE else "black"
        try:
            san = board.san(move)
            board.push(move)
        except Exception:
            break  # malformed movetext — stop at the last legal ply
        last = (move_no, color, san, move.uci())
    return last + (board.fen(),) if last else None


def last_ply_from_pgn(pgn: Optional[str]) -> Optional[Dict]:
    """The last played ply of a PGN, together with the position it reached.

    Returns ``{move_no, color, san, uci, fen_after}`` — the same shape as an entry
    of :func:`moves_from_pgn`, except the FEN is the position *after* the ply (hence
    ``fen_after``): what the board actually shows now. ``move_no``/``color`` describe
    the move that was *played*, so they are the counterpart of the side now *to
    move* — derive the latter with :func:`fullmove_number` / :func:`active_color` on
    ``fen_after`` rather than adjusting these by hand. Returns ``None`` when the PGN
    is missing, unparseable or carries no moves.

    Exists so a caller that only needs "where is this game now" replays the PGN once
    instead of pairing :func:`moves_from_pgn` with :func:`positions_from_pgn`.
    """
    if not pgn:
        return None
    found = _last_ply_tuple(pgn)
    return dict(zip(_LAST_PLY_KEYS, found)) if found else None


def moves_from_pgn(pgn: Optional[str]) -> List[Dict]:
    """Expand a PGN into the played move list, one entry per half-move (ply).

    Each entry is ``{move_no, color, san, uci, fen_before}`` where ``fen_before`` is
    the position with the side-to-move about to play — the same position the coach
    analyses, which is what lets a suggestion be tied back to its move. Returns an
    empty list when the PGN is missing or unparseable.
    """
    if not pgn:
        return []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        return []
    if game is None:
        return []

    board = game.board()
    moves: List[Dict] = []
    for move in game.mainline_moves():
        color = "white" if board.turn == chess.WHITE else "black"
        move_no = board.fullmove_number
        fen_before = board.fen()
        try:
            san = board.san(move)
        except Exception:
            break  # malformed movetext — stop at the last valid ply
        moves.append(
            {
                "move_no": move_no,
                "color": color,
                "san": san,
                "uci": move.uci(),
                "fen_before": fen_before,
            }
        )
        board.push(move)
    return moves


def positions_from_pgn(pgn: Optional[str]) -> List[str]:
    """Replay a PGN and return the FEN after each ply, initial position first.

    ``positions[0]`` is the starting position and ``positions[i]`` is the position
    after ``i`` plies — so a game of ``N`` plies yields ``N + 1`` FENs. The review
    page renders the board at any selected move straight from this list, so the
    client never has to re-implement move legality (castling, promotion, en
    passant). Returns ``[]`` when the PGN is missing or unparseable — mirroring
    ``moves_from_pgn`` so the two stay index-aligned (``positions[i + 1]`` is the
    position reached by ``moves[i]``).
    """
    if not pgn:
        return []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        return []
    if game is None:
        return []

    board = game.board()
    positions = [board.fen()]
    for move in game.mainline_moves():
        try:
            board.push(move)
        except Exception:
            break  # malformed movetext — stop at the last legal ply
        positions.append(board.fen())
    return positions


def annotate_moves(moves: List[Dict], suggestions) -> List[Dict]:
    """Tag each move with the coach analysis requested at that position, if any.

    Joins on ``(move_no, color)`` — a unique key for a ply within a game — derived
    from each suggestion's stored FEN. This is robust to FEN-formatting differences
    (en-passant/halfmove clock) between Chess.com and python-chess. Mutates and
    returns ``moves``, adding ``suggestion`` (object or ``None``), ``analyzed`` and
    ``followed`` (the coach's best move equals the move actually played).
    """
    by_key: Dict[tuple, object] = {}
    for s in suggestions:
        fen = getattr(s, "fen", None)
        move_no = getattr(s, "move_no", None) or fullmove_number(fen)
        by_key[(move_no, active_color(fen))] = s

    for m in moves:
        s = by_key.get((m["move_no"], m["color"]))
        best = getattr(s, "best_move_san", None) if s is not None else None
        m["suggestion"] = s
        m["analyzed"] = s is not None
        m["followed"] = bool(best and best == m["san"])
    return moves
