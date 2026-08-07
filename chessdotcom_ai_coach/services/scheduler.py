"""Scheduler tick body, split into its two steps so each is unit-testable
without a running APScheduler: `sync_current_games` pulls each linked user's
games from Chess.com into the local DB, and `enqueue_due_analyses` selects
games due for analysis from that local DB and enqueues Celery tasks.
`run_scheduler` calls them back-to-back on every tick.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import CoachSuggestion, Game, User
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store
from .analysis import enqueue_game_analysis
from .chess_client import Client

logger = logging.getLogger(__name__)

# Only games that ended within this window are retried against the archives, so a
# game that never resolves (e.g. played under a different alias) is not re-fetched
# on every tick forever.
RESULT_BACKFILL_WINDOW = timedelta(days=3)

# How long a suggestion may sit PENDING before it's assumed lost and re-enqueued.
# This must comfortably exceed the worst-case *queue* wait, not just one analysis:
# a whole-game backfill is ~40 moves x (2s Stockfish + up to 150s LLM), so a task
# can legitimately wait far longer than it takes to run. Too low a value re-queues
# tasks that are still alive and grows the very backlog it's reacting to.
STALE_PENDING_AFTER = timedelta(minutes=30)

# Give up on a position after this many hand-offs to the worker, so one that fails
# every time (unparseable FEN, engine that won't start) stops looping.
MAX_ANALYSIS_ATTEMPTS = 3

# Cap the rows revived per tick so a large backlog is drained gradually rather
# than dumped on the worker in one go.
REQUEUE_BATCH_SIZE = 20


def _linked_users():
    """Active users who linked a Chess.com account (the ones worth polling)."""
    return (
        User.objects.filter(is_active=True)
        .exclude(chessdotcom_username__isnull=True)
        .exclude(chessdotcom_username="")
    )


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
    for user in _linked_users():
        try:
            games = Client(username=user.chess_username).my_current_games()
            game_store.upsert_current_games(user, games)
        except Exception:
            logger.exception("Chess.com sync failed for user %s", user.chess_username)


def backfill_results() -> int:
    """Resolve recently-ended games from the archives, then analyse every user move.

    A game only leaves Chess.com's "current games" once it's over, and the snapshot
    we kept has a PGN with Result "*", so the outcome must be fetched separately.
    For each linked user with finished-but-unresolved games in the backfill window,
    pull the current month's archive (falling back to the previous month for games
    not found there, to cover month boundaries) and persist each match. A per-user
    failure is logged and skipped so one bad account doesn't block the batch.
    Returns the number of games resolved this tick.

    This is also where a finished game gets its *complete* analysis. `enqueue_due_analyses`
    only ever sees the position a 5s poll happens to catch, so any turn that comes
    and goes between two ticks is never analysed — in fast time controls that's most
    of them. Here the archive has just given us the final PGN, so the whole move list
    is known and `enqueue_game_analysis` can fill in what the live path missed.
    Doing it after `set_result` matters: the refreshed PGN is what makes the backfill
    complete rather than stopping at our possibly-truncated snapshot.

    Known gap: only games that resolve within `RESULT_BACKFILL_WINDOW` get here, so a
    game that never matches an archive entry (e.g. played under a different alias) is
    never backfilled — `manage.py analyze_game` remains the manual escape hatch.
    """
    updated = 0
    since = timezone.now() - RESULT_BACKFILL_WINDOW
    for user in _linked_users():
        try:
            pending = game_store.unresolved_past_games(user, since)
            if not pending:
                continue
            client = Client(username=user.chess_username)
            results = client.finished_game_results()  # current month
            if any(game.game_id not in results for game in pending):
                # Some games ended in a prior month (e.g. long daily games): merge
                # in last month's archive, keeping the current month's entries.
                prev_month_end = timezone.now().replace(day=1) - timedelta(days=1)
                results = {
                    **client.finished_game_results(
                        prev_month_end.year, prev_month_end.month
                    ),
                    **results,
                }
            for game in pending:
                match = results.get(game.game_id)
                if match:
                    game_store.set_result(
                        user,
                        game.game_id,
                        match["result"],
                        match["detail"],
                        match.get("pgn", ""),
                    )
                    enqueue_game_analysis(user, game.game_id)
                    updated += 1
        except Exception:
            logger.exception(
                "Result backfill failed for user %s", user.chess_username
            )
    return updated


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
                "attempts": 1,  # creating the row is itself a hand-off to the worker
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


def requeue_stale_analyses() -> int:
    """Revive suggestions left PENDING by a task that never came back.

    The `CoachSuggestion` row doubles as the in-flight lock, so a task lost with
    its worker (an OOM kill, say — Celery does not redeliver by default) strands
    the row PENDING for ever: every later `get_or_create` finds it and enqueues
    nothing, and the coach card spins on its self-poll with no way out. This gives
    the lock an expiry.

    A row untouched for `STALE_PENDING_AFTER` is handed to the worker again and its
    `attempts` bumped — the save also refreshes `updated_at`, which spaces out the
    next retry. Past `MAX_ANALYSIS_ATTEMPTS` the position is closed as DONE with the
    same "unavailable" shape `coach.get_best_move` produces on error, so the spinner
    stops without inventing a new status. Re-enqueuing a task that was in fact still
    alive is wasteful but harmless: `analyze_game_task` upserts on the same key.
    Returns the number of rows re-enqueued this tick.
    """
    cutoff = timezone.now() - STALE_PENDING_AFTER
    stale = CoachSuggestion.objects.filter(
        status=CoachSuggestion.Status.PENDING, updated_at__lt=cutoff
    )[:REQUEUE_BATCH_SIZE]

    requeued = 0
    for row in stale:
        if row.attempts >= MAX_ANALYSIS_ATTEMPTS:
            logger.warning(
                "Giving up on analysis for game %s move %s after %d attempts",
                row.game_id,
                row.move_no,
                row.attempts,
            )
            row.status = CoachSuggestion.Status.DONE
            row.eval_text = "Analysis unavailable."
            row.analysis = (
                "The coach could not analyse this position: the background "
                f"analysis did not complete after {row.attempts} attempts."
            )
            row.save()
            continue

        game = Game.objects.filter(user_id=row.user_id, game_id=row.game_id).first()
        row.attempts += 1
        row.save()  # `auto_now` on updated_at: this also defers the next retry
        analyze_game_task.delay(
            row.user_id, row.game_id, row.fen, (game.pgn or None) if game else None
        )
        requeued += 1
    return requeued
