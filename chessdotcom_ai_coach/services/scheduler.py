"""Scheduler tick body, split into its two steps so each is unit-testable
without a running APScheduler: `sync_current_games` pulls each linked user's
games from Chess.com into the local DB, and `enqueue_due_analyses` selects
games due for analysis from that local DB and enqueues Celery tasks.
`run_scheduler` calls them back-to-back on every tick.
"""

import logging

from ..models import CoachSuggestion, Game, User
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store
from .chess_client import Client

logger = logging.getLogger(__name__)


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


def sync_current_games() -> None:
    """Refresh linked users' current games from Chess.com into the local DB.

    This is the only path that keeps `Game` fresh now that the home page is a
    plain DB read (see `views.home`/`views.game_list`) — without it, `Game`
    rows would never advance and `enqueue_due_analyses` would keep checking a
    stale FEN. Only users who explicitly linked a Chess.com account are
    synced. A per-user failure (bad username, transient network error) is
    logged and skipped so it doesn't block the rest of the batch.
    """
    users = User.objects.filter(is_active=True).exclude(
        chessdotcom_username__isnull=True
    ).exclude(chessdotcom_username="")
    for user in users:
        try:
            games = Client(username=user.chess_username).my_current_games()
            game_store.upsert_current_games(user, games)
        except Exception:
            logger.exception("Chess.com sync failed for user %s", user.chess_username)


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
