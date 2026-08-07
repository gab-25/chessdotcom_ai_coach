"""Enqueue coach analysis for a whole game.

The live path can only analyse the position a 5s poll happens to catch, so any
turn that comes and goes between two ticks is never seen — in fast time controls
that's most of them. This fills in the rest, so a game can be reviewed with the
coach's take on *every* one of the user's moves. Shared by
``scheduler.backfill_results`` (automatic, once a finished game resolves and the
archive has given us the final PGN) and the ``analyze_game`` management command
(manual), so both paths enqueue identically. Reads the stored ``Game`` snapshot
only — no Chess.com call.
"""

from __future__ import annotations

from ..models import CoachSuggestion
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store


def _user_orientation(user, game) -> str:
    return "white" if (game.white_name or "").lower() == user.chess_username.lower() else "black"


def _ply_already_covered(user, game_id: str, fen: str) -> bool:
    """True when a suggestion for this ply exists, whatever FEN spelling it used.

    The unique key is the raw FEN, but the same ply can be stored under two
    spellings: the live scheduler saves Chess.com's FEN while this module saves
    python-chess's ``board.fen()``, and the two can differ in the halfmove clock
    or the en-passant field. Matching on ``(move_no, side to move)`` — the ply
    identity ``board_utils.annotate_moves`` already joins on — keeps the backfill
    from re-analysing every move the coach handled live.
    """
    move_no = board_utils.fullmove_number(fen)
    if move_no is None:
        # No ply identity to match on — a NULL `move_no` filter would sweep in
        # unrelated rows, so leave it to the unique key on the FEN.
        return False
    rows = CoachSuggestion.objects.filter(user=user, game_id=game_id, move_no=move_no)
    color = board_utils.active_color(fen)
    return any(board_utils.active_color(row.fen) == color for row in rows)


def enqueue_game_analysis(user, game_id: str):
    """Queue analysis for every un-analysed move the user played in ``game_id``.

    For each of the user's moves we enqueue the same Celery task the live coach
    uses, keyed by the position the user was about to play (``fen_before``). It is
    idempotent on two levels: ``_ply_already_covered`` skips a move the live coach
    already analysed under Chess.com's FEN spelling, and ``get_or_create`` on
    ``(user, game_id, fen)`` leaves an existing pending/completed row alone — so
    re-running is safe and cheap. Returns ``{"enqueued", "total", "game"}`` or
    ``None`` when the game isn't stored for the user.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None

    orientation = _user_orientation(user, game)
    user_moves = [m for m in board_utils.moves_from_pgn(game.pgn) if m["color"] == orientation]

    enqueued = 0
    for move in user_moves:
        fen = move["fen_before"]
        if _ply_already_covered(user, game_id, fen):
            continue
        _row, created = CoachSuggestion.objects.get_or_create(
            user=user,
            game_id=game_id,
            fen=fen,
            defaults={
                "status": CoachSuggestion.Status.PENDING,
                "move_no": board_utils.fullmove_number(fen),
                "attempts": 1,  # creating the row is itself a hand-off to the worker
                "eval_text": "",
                "analysis": "",
            },
        )
        if created:
            analyze_game_task.delay(user.id, game_id, fen, game.pgn or None)
            enqueued += 1

    return {"enqueued": enqueued, "total": len(user_moves), "game": game}
