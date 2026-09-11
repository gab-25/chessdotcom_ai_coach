"""Scheduler job bodies, split into steps so each is unit-testable without a
running APScheduler.

One schedule calls in here (see `management.commands.run_scheduler`):

* `import_archive_month` pulls one month of each linked user's Chess.com archive
  into the local DB — every finished game, live and daily alike. This is where
  the app's games come from.
* `sync_current_games` covers the one gap the archive leaves: a daily game still
  being played is not in it, and this is what notices when such a game ends.
* `requeue_stale_analyses` / `requeue_orphaned_analyses` revive analyses that got
  stuck.

**Nothing here enqueues analysis.** A full archive is thousands of games at
dozens of analyses each, which no worker is going to finish, so the user asks for
a game to be analysed and `analysis.enqueue_game_analysis` is called from the
view. These jobs only gather games and unstick work that was already requested.

The import is deliberately paced at **one month per user per run**: Chess.com
does not take kindly to bursts, and a multi-year account is dozens of monthly
archives. `ArchiveImport` records how far each user has got, so a run picks up
where the last one stopped instead of starting over.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import MAX_ANALYSIS_ATTEMPTS, ArchiveImport, CoachSuggestion, Game, User
from ..tasks import analyze_game_task
from . import game_store
from .chess_client import Client

logger = logging.getLogger(__name__)

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


def linked_users():
    """Active users who linked a Chess.com account (the ones worth polling).

    Public because `manage.py import_archives` needs the same set: a user without
    a linked account has no archive to read.
    """
    return (
        User.objects.filter(is_active=True)
        .exclude(chessdotcom_username__isnull=True)
        .exclude(chessdotcom_username="")
    )


def sync_current_games() -> None:
    """Refresh linked users' current games from Chess.com into the local DB.

    Narrow job, easy to over-read: the endpoint behind it is **daily-only and
    in-progress-only**, so it is not how games get here — `import_archive_month`
    is. What it does is flip `is_active` to False when a daily game disappears
    from the current-games list, which is what stops a finished game being hidden
    as "still in progress" until the next archive read catches up.

    Only users who explicitly linked a Chess.com account are synced. A per-user
    failure (bad username, transient network error) is logged and skipped so it
    doesn't block the rest of the batch.
    """
    for user in linked_users():
        try:
            games = Client(username=user.chess_username).my_current_games()
            game_store.upsert_current_games(user, games)
        except Exception:
            logger.exception("Chess.com sync failed for user %s", user.chess_username)


def _months_to_import(user, months: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Which archive months this run should read for `user`, at most two.

    The **current month is always read**: it keeps growing as the user plays, so
    a game finished minutes ago is in it and nowhere else. That is what makes a
    finished game show up without any other mechanism watching for it.

    On top of that, one month of backlog is taken per run — the newest not yet
    imported, so a fresh account fills in with recent games first and works
    backwards, instead of making the user wait while years of history scroll by.
    Two requests per run while there is history left, one once there isn't.
    """
    if not months:
        return []

    now = timezone.now()
    current = (now.year, now.month)
    due = [current] if current in months else []

    done = {(row.year, row.month) for row in ArchiveImport.objects.filter(user=user)}
    backlog = [m for m in months if m not in done and m != current]
    if backlog:
        due.append(backlog[-1])  # `months` is ascending, so this is the newest
    return due


def import_archive_month() -> int:
    """Import due archive months for every linked user. Returns games added.

    This is where the app's games come from: the monthly archive is the only
    endpoint that carries live games (bullet, blitz, rapid) at all, and it also
    holds finished daily games with their final PGN and result.

    Paced at one backlog month per run on purpose. A five-year account is ~60
    monthly archives; fetching them in one go would both hammer Chess.com and
    write thousands of rows at once. `ArchiveImport` records what has been read,
    so successive runs walk backwards through the history and then settle into
    re-reading just the current month. See `_months_to_import`.

    A per-user failure (bad username, transient network error) is logged and
    skipped so one bad account doesn't block the batch.
    """
    added = 0
    for user in linked_users():
        try:
            client = Client(username=user.chess_username)
            for year, month in _months_to_import(user, client.archive_months()):
                games = client.finished_games(year, month)
                added += game_store.upsert_finished_games(user, games)
                ArchiveImport.objects.update_or_create(
                    user=user,
                    year=year,
                    month=month,
                    defaults={"game_count": len(games)},
                )
                logger.info(
                    "Imported %d games for %s from %04d-%02d",
                    len(games),
                    user.chess_username,
                    year,
                    month,
                )
        except Exception:
            logger.exception("Archive import failed for user %s", user.chess_username)
    return added


def import_all_archives(user, months: int | None = None) -> int:
    """Read a user's whole archive in one go. Returns the number of games added.

    The bulk catch-up behind `manage.py import_archives`, for when waiting for
    the scheduler to walk a month per run is not worth it. Requests are made one
    month at a time in sequence — Chess.com tolerates that far better than
    parallel fetches. ``months`` caps how many of the most recent months are
    read; None means the whole history.
    """
    client = Client(username=user.chess_username)
    wanted = client.archive_months()
    if months is not None:
        wanted = wanted[-months:]

    added = 0
    for year, mm in reversed(wanted):  # newest first: recent games show up first
        games = client.finished_games(year, mm)
        added += game_store.upsert_finished_games(user, games)
        ArchiveImport.objects.update_or_create(
            user=user, year=year, month=mm, defaults={"game_count": len(games)}
        )
        logger.info(
            "Imported %d games for %s from %04d-%02d",
            len(games),
            user.chess_username,
            year,
            mm,
        )
    return added


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
