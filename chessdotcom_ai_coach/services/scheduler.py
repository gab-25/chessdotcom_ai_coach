"""Scheduler poll body: select games due for analysis and enqueue Celery tasks.

Kept separate from the management command so it can be unit-tested without a
running APScheduler. Reads only the local DB (no Chess.com API call), so a 1s
poll is a single indexed query over active games.
"""

from ..models import CoachSuggestion, Game
from ..tasks import analyze_game_task
from . import board as board_utils


def _user_color(game: Game) -> str | None:
    """Which color the game's user plays ("white"/"black"), or None if unknown."""
    username = game.user.chess_username.lower()
    if game.white_name and game.white_name.lower() == username:
        return "white"
    if game.black_name and game.black_name.lower() == username:
        return "black"
    return None


def _is_user_turn(game: Game) -> bool:
    """True when the side to move in `game.fen` is the side the user plays."""
    color = _user_color(game)
    return color is not None and board_utils.active_color(game.fen) == color


def enqueue_due_analyses() -> int:
    """Enqueue analysis for every active game where it's the user's turn.

    Dedup: a pending/done `CoachSuggestion` row for (user, game_id, fen) means the
    position is already queued or analysed, so `get_or_create` only enqueues when
    the row was just created. Returns the number of tasks enqueued this tick.
    """
    enqueued = 0
    games = Game.objects.filter(is_active=True).select_related("user")
    for game in games:
        if not game.fen or not _is_user_turn(game):
            continue

        _row, created = CoachSuggestion.objects.get_or_create(
            user=game.user,
            game_id=game.game_id,
            fen=game.fen,
            defaults={
                "status": CoachSuggestion.Status.PENDING,
                "move_no": board_utils.fullmove_number(game.fen),
                "eval_text": "",
                "analysis": "",
            },
        )
        if created:
            analyze_game_task.delay(
                game.user_id, game.game_id, game.fen, game.pgn or None
            )
            enqueued += 1
    return enqueued
