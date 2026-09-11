"""Enqueue coach analysis for a whole game.

Analysis is keyed on the moves the user actually played, read from the stored
PGN, so a game can be reviewed with the coach's take on *every* one of them. A
position the user never played is not analysed — there is nothing in the PGN to
analyse it against.

It is written to be run repeatedly rather than once: enqueuing is idempotent, so
the same call reconciles a game towards "every user move analysed" no matter how
much of it is already done. That is what lets ``views.analyze_game`` sit behind a
button nobody has to press carefully — a second press queues nothing — and what
makes the ``analyze_game`` management command safe to re-run. Reads the stored
``Game`` row only, never Chess.com.
"""

from __future__ import annotations

from ..models import CoachSuggestion
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store


def _user_orientation(user, game) -> str:
    return "white" if (game.white_name or "").lower() == user.chess_username.lower() else "black"


def _covered_plies(user, game_id: str) -> set[tuple[int, str]]:
    """The plies already carrying a row, as ``{(move_no, side to move)}``.

    The unique key is the raw FEN, but the same ply can be stored under two
    spellings: this module saves python-chess's ``board.fen()``, while rows left
    over from the app's earlier live path hold Chess.com's spelling of the same
    position, and the two can differ in the halfmove clock or the en-passant
    field. Matching on ``(move_no, side to move)`` — the ply identity
    ``board_utils.annotate_moves`` already joins on — stops a second request
    re-analysing a move that already has a row.

    Built in one query for the whole game rather than one per move.
    """
    covered: set[tuple[int, str]] = set()
    rows = CoachSuggestion.objects.filter(user=user, game_id=game_id).only(
        "fen", "move_no"
    )
    for row in rows:
        move_no = row.move_no or board_utils.fullmove_number(row.fen)
        if move_no is None:
            continue  # no ply identity to match on — the FEN key still guards it
        covered.add((move_no, board_utils.active_color(row.fen)))
    return covered


def enqueue_game_analysis(user, game_id: str):
    """Queue analysis for every un-analysed move the user played in ``game_id``.

    One Celery task per move, keyed by the position the user was about to play
    (``fen_before``). Idempotent on two levels: ``_covered_plies`` skips a move
    already analysed under a different FEN spelling, and ``get_or_create`` on
    ``(user, game_id, fen)`` leaves an existing row alone. Returns
    ``{"enqueued", "total", "game"}`` — the counts the caller shows as progress —
    or ``None`` when the game isn't stored for the user.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None

    orientation = _user_orientation(user, game)
    user_moves = [m for m in board_utils.moves_from_pgn(game.pgn) if m["color"] == orientation]
    covered = _covered_plies(user, game_id)

    enqueued = 0
    for move in user_moves:
        fen = move["fen_before"]
        move_no = board_utils.fullmove_number(fen)
        if (move_no, board_utils.active_color(fen)) in covered:
            continue
        _row, created = CoachSuggestion.objects.get_or_create(
            user=user,
            game_id=game_id,
            fen=fen,
            defaults={
                "status": CoachSuggestion.Status.PENDING,
                "move_no": move_no,
                "eval_text": "",
                "analysis": "",
            },
        )
        if created:
            analyze_game_task.delay(user.id, game_id, fen, game.pgn or None)
            enqueued += 1

    return {"enqueued": enqueued, "total": len(user_moves), "game": game}
