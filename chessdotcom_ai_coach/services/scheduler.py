"""Scheduler job bodies, split into steps so each is unit-testable without a
running APScheduler.

Two schedules call in here (see `management.commands.run_scheduler`):

* the 5s tick — `sync_current_games` pulls each linked user's games from
  Chess.com into the local DB, `backfill_results` resolves the outcome of games
  that just ended, `enqueue_due_analyses` enqueues analysis for the active games,
  and `requeue_stale_analyses` / `requeue_orphaned_analyses` revive analyses that
  got stuck;
* the 10 minute scan — `enqueue_finished_game_analyses` reconciles finished
  games towards "every user move analysed".

The two enqueue steps are reconciliation passes, not one-shot triggers: each
compares what the PGN says the user played against the `CoachSuggestion` rows
that exist and queues the difference. Anything missed — because a poll landed
between two moves, because a task was lost, because the worker was down — is
therefore picked up on a later run rather than being gone for good.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import MAX_ANALYSIS_ATTEMPTS, CoachSuggestion, Game, User
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store
from .analysis import enqueue_game_analysis
from .chess_client import Client

logger = logging.getLogger(__name__)

# Only games that ended within this window are retried against the archives, so a
# game that never resolves (e.g. played under a different alias) is not re-fetched
# on every tick forever. It bounds the Chess.com calls only — the analysis scan
# below deliberately has no such window.
RESULT_BACKFILL_WINDOW = timedelta(days=3)

# How long an analysis may stay RUNNING before it's assumed dead. This bounds a
# *single* analysis, not a queue wait: `coach.get_best_move` is capped at 2s of
# Stockfish plus a 150s LLM timeout, so ten minutes is already a wide margin.
# Queued work is not measured against it — a PENDING row has no worker on it yet
# and is recovered by the broker redelivering the message (`task_acks_late`).
ANALYSIS_TIMEOUT = timedelta(minutes=10)

# Cap the rows revived per tick so a large backlog is drained gradually rather
# than dumped on the worker in one go.
REQUEUE_BATCH_SIZE = 20

# The queue Celery consumes from, as `requeue_orphaned_analyses` needs to read
# its depth. `task_default_queue`, which nothing overrides.
TASK_QUEUE_NAME = "celery"

# Cap the tasks a single scan of the finished games may enqueue. The scan covers
# the whole history, so the first run after a long outage would otherwise dump
# thousands of analyses on the worker at once; the leftovers are picked up ten
# minutes later, since the scan is a reconciliation and not a one-shot trigger.
ENQUEUE_BUDGET_PER_RUN = 200


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
    """Resolve the outcome of recently-ended games from the Chess.com archives.

    A game only leaves Chess.com's "current games" once it's over, and the snapshot
    we kept has a PGN with Result "*", so the outcome must be fetched separately.
    For each linked user with finished-but-unresolved games in the backfill window,
    pull the current month's archive (falling back to the previous month for games
    not found there, to cover month boundaries) and persist each match. A per-user
    failure is logged and skipped so one bad account doesn't block the batch.
    Returns the number of games resolved this tick.

    The archive's PGN is written along with the result: our own snapshot stops at
    the last sync before the game left "current games", so it can be missing the
    closing moves. That refreshed movetext is what lets
    `enqueue_finished_game_analyses` see the full move list — but the analysis
    itself is not enqueued here. A game whose result never resolves (played under
    a different alias, say) would then never be analysed at all; the scan runs off
    the stored PGN instead and covers it regardless.
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
                    updated += 1
        except Exception:
            logger.exception(
                "Result backfill failed for user %s", user.chess_username
            )
    return updated


def enqueue_due_analyses() -> int:
    """Enqueue the outstanding analyses of every active game.

    Two things are due while a game is running. The position the user is *about*
    to play, which only exists in the live FEN and never reaches the PGN as
    something to analyse — that's the `get_or_create` below, guarded by
    `_is_user_turn`. And the moves already played: a 5s poll only ever sees the
    position it happens to land on, so in fast time controls most turns come and
    go between two ticks and would stay un-analysed until the game ended.
    `enqueue_game_analysis` reconciles those from the PGN on every tick, so a
    missed move is picked up within seconds instead of after the game.

    Dedup: a `CoachSuggestion` row for (user, game_id, fen) means the position is
    already queued, running or analysed, so `get_or_create` only enqueues when the
    row was just created. Returns the number of tasks enqueued this tick.
    """
    enqueued = 0
    games = Game.objects.filter(is_active=True).select_related("user")
    for game in games:
        if game.fen and _is_user_turn(game):
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

        result = enqueue_game_analysis(game.user, game.game_id)
        if result:
            enqueued += result["enqueued"]
    return enqueued


def enqueue_finished_game_analyses() -> int:
    """Enqueue the analyses still missing from finished games.

    The live path can only cover a game while it is being played, and anything it
    missed — a move played between two ticks, a task lost to a worker restart, a
    game that was already over when the account was linked — needs a second look.
    This is that second look: every finished game is compared against its
    `CoachSuggestion` rows and the difference is queued. It runs on its own
    10 minute schedule rather than on the 5s tick because it reads every stored
    game, and nothing about a finished game changes fast enough to need more.

    Deliberately unbounded in time: a game is checked for as long as it is stored,
    so one whose result never resolved from the archives still gets analysed.
    `ENQUEUE_BUDGET_PER_RUN` bounds the work instead — the leftovers come back on
    the next run. A per-user failure is logged and skipped. Returns the number of
    tasks enqueued this run.
    """
    enqueued = 0
    for user in _linked_users():
        try:
            for game in game_store.past_games(user):
                if not game.pgn:
                    continue  # nothing to reconcile against
                budget = ENQUEUE_BUDGET_PER_RUN - enqueued
                if budget <= 0:
                    logger.info(
                        "Finished-game scan hit its budget of %d tasks; "
                        "the rest follows on the next run",
                        ENQUEUE_BUDGET_PER_RUN,
                    )
                    return enqueued
                result = enqueue_game_analysis(user, game.game_id, limit=budget)
                if result:
                    enqueued += result["enqueued"]
        except Exception:
            logger.exception(
                "Finished-game analysis scan failed for user %s", user.chess_username
            )
    return enqueued


def requeue_stale_analyses() -> int:
    """Revive analyses whose worker started and never came back.

    The `CoachSuggestion` row doubles as the in-flight lock, so a RUNNING row that
    is never completed strands the position: every later `get_or_create` finds it
    and enqueues nothing, and the coach card spins on its self-poll with no way
    out. This gives the lock an expiry.

    Only RUNNING rows are swept. A PENDING row is merely queued — it can sit there
    far longer than an analysis takes when a whole-game scan is draining — and is
    the broker's business: `task_acks_late` means the message is redelivered if
    the worker dies with it. A row that has been RUNNING past `ANALYSIS_TIMEOUT`
    has no such excuse, so it goes back to PENDING and onto the queue again; the
    save refreshes `updated_at`, which also spaces out the next sweep. The retry
    itself is counted by `analyze_game_task` when a worker picks the row up, so a
    re-queue that turns out to be unnecessary costs nothing but a duplicate
    message (the task upserts on the same key). Past `MAX_ANALYSIS_ATTEMPTS` the
    position is retired as FAILED so the spinner stops. Returns the number of rows
    re-enqueued this tick.
    """
    cutoff = timezone.now() - ANALYSIS_TIMEOUT
    stale = CoachSuggestion.objects.filter(
        status=CoachSuggestion.Status.RUNNING, updated_at__lt=cutoff
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
            row.status = CoachSuggestion.Status.FAILED
            row.save()
            continue

        game = Game.objects.filter(user_id=row.user_id, game_id=row.game_id).first()
        row.status = CoachSuggestion.Status.PENDING
        row.save()  # `auto_now` on updated_at: this also defers the next sweep
        analyze_game_task.delay(
            row.user_id, row.game_id, row.fen, (game.pgn or None) if game else None
        )
        requeued += 1
    return requeued


def _queued_task_count() -> int | None:
    """How many messages are waiting on the broker, or None if it can't be read.

    Reads the queue Celery actually consumes from. Returning None on any error is
    deliberate: the one caller treats "unknown" as "assume there is work", so a
    broker hiccup can never be mistaken for an empty queue.
    """
    try:
        from ..celery import app

        with app.connection_or_acquire() as conn:
            return conn.default_channel.client.llen(TASK_QUEUE_NAME)
    except Exception:
        logger.exception("Could not read the broker queue depth")
        return None


def requeue_orphaned_analyses() -> int:
    """Re-enqueue PENDING rows that have no message waiting for them.

    `requeue_stale_analyses` covers a row whose *worker* died. This covers the
    other half: a row whose *message* is gone, which nothing else can recover.
    The row still says PENDING, so every `get_or_create` in the reconciliation
    passes finds it and enqueues nothing — the position is locked by work that
    does not exist, for ever. Redis losing the queue (it holds no volume, so a
    container restart empties it) is the ordinary way to get there.

    Detection is by comparison rather than by age, because age cannot tell the
    two apart: draining a whole-game scan legitimately leaves a row queued for
    hours. But if the broker's queue is empty and no worker is running anything,
    then no PENDING row can have a message — whatever its age. That is exact, so
    there are no false positives and recovery takes one tick.

    Deliberately conservative: it does nothing while the queue depth is unknown
    or non-zero, and no attempt is counted, since nothing was attempted. Returns
    the number of rows re-enqueued this tick.
    """
    if CoachSuggestion.objects.filter(status=CoachSuggestion.Status.RUNNING).exists():
        return 0  # a worker is busy, so the queue is being served

    depth = _queued_task_count()
    if depth is None or depth > 0:
        return 0  # unreadable, or there really is work waiting

    orphaned = CoachSuggestion.objects.filter(
        status=CoachSuggestion.Status.PENDING
    ).order_by("updated_at")[:REQUEUE_BATCH_SIZE]

    requeued = 0
    for row in orphaned:
        game = Game.objects.filter(user_id=row.user_id, game_id=row.game_id).first()
        row.save()  # `auto_now` on updated_at: records that we handed it over
        analyze_game_task.delay(
            row.user_id, row.game_id, row.fen, (game.pgn or None) if game else None
        )
        requeued += 1
    if requeued:
        logger.warning(
            "Re-enqueued %d analyses that were PENDING with an empty queue", requeued
        )
    return requeued
