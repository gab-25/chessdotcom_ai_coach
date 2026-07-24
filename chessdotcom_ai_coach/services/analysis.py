"""Enqueue coach analysis for a whole game.

The scheduler only analyses the position it's a user's turn to play; this fills
in the rest so a finished (or in-progress) game can be reviewed with the coach's
take on *every* one of the user's moves. Shared by the ``analyze_game``
management command (and, later, the in-page "Analyze game" button) so both paths
enqueue identically. Reads the stored ``Game`` snapshot only — no Chess.com call.
"""

from __future__ import annotations

from ..models import CoachSuggestion
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store


def _user_orientation(user, game) -> str:
    return "white" if (game.white_name or "").lower() == user.chess_username.lower() else "black"


def enqueue_game_analysis(user, game_id: str):
    """Queue analysis for every un-analysed move the user played in ``game_id``.

    For each of the user's moves we enqueue the same Celery task the live coach
    uses, keyed by the position the user was about to play (``fen_before``).
    ``get_or_create`` on ``(user, game_id, fen)`` makes it idempotent: a move that
    already has a pending or completed suggestion is left alone, so re-running is
    safe and cheap. Returns ``{"enqueued", "total", "game"}`` or ``None`` when the
    game isn't stored for the user.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None

    orientation = _user_orientation(user, game)
    user_moves = [m for m in board_utils.moves_from_pgn(game.pgn) if m["color"] == orientation]

    enqueued = 0
    for move in user_moves:
        fen = move["fen_before"]
        _row, created = CoachSuggestion.objects.get_or_create(
            user=user,
            game_id=game_id,
            fen=fen,
            defaults={
                "status": CoachSuggestion.Status.PENDING,
                "move_no": board_utils.fullmove_number(fen),
                "eval_text": "",
                "analysis": "",
            },
        )
        if created:
            analyze_game_task.delay(user.id, game_id, fen, game.pgn or None)
            enqueued += 1

    return {"enqueued": enqueued, "total": len(user_moves), "game": game}
