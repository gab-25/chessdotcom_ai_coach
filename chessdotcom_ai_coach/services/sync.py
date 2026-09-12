"""Keeping a user's games and analyses up to date, driven by their own requests.

There is **no scheduler**. Everything here is reached from the request path, for
the user making the request, which is what lets the web container scale to as
many replicas as it likes: a background scheduler started inside that container
would run once per replica and fetch every archive that many times over.

Two entry points, and they deliberately run in different places:

* `request_sync` — takes a per-user claim and hands the archive import to the
  Celery worker (`tasks.sync_user_task`). The import is slow IO (one HTTP request
  per archive month), so it must not happen inside the request.
* `recover_stuck_analyses` — runs the two recovery sweeps *inline* in the web
  process, from the detail page's Refresh button. They are cheap (one indexed
  query, one Redis `LLEN`), and more to the point `requeue_stale_analyses` exists
  to rescue analyses from a wedged worker: queued behind that same worker it
  would be unable to run in exactly the case it was written for.

**Nothing here enqueues analysis of its own accord.** A full archive is thousands
of games at dozens of analyses each, which no worker is going to finish, so the
user asks for a game to be analysed and `analysis.enqueue_game_analysis` is
called from the view. This module only gathers games and unsticks work that was
already requested.

Games in progress are not tracked. Earlier versions snapshotted them from the
*current games* endpoint on the belief that a game not caught before it ended was
lost for good — the archives disprove it: they carry every finished game with its
final PGN, so the snapshot was only ever an incomplete copy of what arrives
anyway.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
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

# Cap the rows revived per sweep so a large backlog is drained gradually rather
# than dumped on the worker in one go.
REQUEUE_BATCH_SIZE = 20

# The queue Celery consumes from, as `requeue_orphaned_analyses` needs to read
# its depth. `task_default_queue`, which nothing overrides.
TASK_QUEUE_NAME = "celery"

# How long a user's archive sync claim holds. An incremental sync is two HTTP
# requests (the archive index plus the current month), and it is now paid only
# when somebody presses Sync — an idle deployment makes no requests at all,
# where the old 10-minute scheduler tick polled every linked user around the
# clock, and merely opening a page no longer costs a fetch either.
SYNC_COOLDOWN = timedelta(seconds=settings.SYNC_COOLDOWN_SECONDS)

# How often the recovery sweeps may run in one web process. They hang off the
# detail page's Refresh button, which a waiting user can press as fast as they
# like, and each run costs a Redis `LLEN`. The throttle is per process rather
# than per user, so a refresh inside the interval can sweep nothing at all — the
# cost of that is one more press, since the sweeps only act on rows RUNNING past
# ANALYSIS_TIMEOUT or on an empty queue, neither of which goes away on its own.
RECOVERY_INTERVAL = timedelta(seconds=30)

# When the sweeps last ran in *this* process. Deliberately not shared across
# gunicorn workers: unlike the archive import, a duplicate sweep costs nothing —
# the first one flips the rows and the second finds none — so a per-process
# throttle and the occasional race are both harmless.
_last_recovery = None


def is_linked(user) -> bool:
    """Whether this user has a Chess.com account to read an archive from.

    Reads the raw field, **not** the `chess_username` fallback. That fallback
    exists so the board can be oriented for a user who never set the field
    (`views._position_context`); it is not a claim that their app username is a
    real Chess.com account. Treating it as one here would mean a lookup for a
    probably-nonexistent player every time somebody presses Sync.
    """
    return bool(user.chessdotcom_username)


def linked_users():
    """Active users who linked a Chess.com account.

    The batch counterpart of `is_linked`, for `manage.py import_archives`, which
    is the one caller that still works over every user rather than the one making
    a request.
    """
    return (
        User.objects.filter(is_active=True)
        .exclude(chessdotcom_username__isnull=True)
        .exclude(chessdotcom_username="")
    )


def _due_months(user, months: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Which archive months a sync should read for `user`.

    Every month the archive offers that has no `ArchiveImport` row, plus the
    **current** month — which keeps growing as the user plays, so a game finished
    minutes ago is in it and nowhere else, and re-reading it is what makes a
    finished game show up without anything else watching for it.

    Expressed as a set difference rather than as a cursor on purpose. It gives
    three behaviours for one rule: a first sync reads the whole history, a later
    one reads only the current month, and a backfill cut short partway through
    resumes at the months it never reached. A "have we imported before?" flag
    would get that last case wrong and strand the rest of the history for good.

    Newest first, matching `import_all_archives`, so recent games land first.
    """
    if not months:
        return []

    current = (timezone.now().year, timezone.now().month)
    done = {(row.year, row.month) for row in ArchiveImport.objects.filter(user=user)}
    due = [m for m in reversed(months) if m not in done or m == current]
    return due


def _import_months(user, client, months: list[tuple[int, int]]) -> int:
    """Read the given archive months into the DB. Returns the games added.

    Requests are made one month at a time in sequence — Chess.com tolerates that
    far better than parallel fetches — and `ArchiveImport` is written as each
    month lands, so an interrupted run keeps what it already read.

    Each month also re-stamps `last_synced_at`. That is the heartbeat on the
    claim `request_sync` took: a first backfill can run for longer than
    `SYNC_COOLDOWN`, and without this the claim would expire mid-run and a second
    sync would start on top of the first, doubling the traffic to Chess.com at
    the very moment it is most likely to rate-limit us.
    """
    added = 0
    for year, month in months:
        games = client.finished_games(year, month)
        added += game_store.upsert_finished_games(user, games)
        ArchiveImport.objects.update_or_create(
            user=user, year=year, month=month, defaults={"game_count": len(games)}
        )
        User.objects.filter(pk=user.pk).update(last_synced_at=timezone.now())
        logger.info(
            "Imported %d games for %s from %04d-%02d",
            len(games),
            user.chess_username,
            year,
            month,
        )
    return added


def sync_user(user) -> int:
    """Bring one user's stored games up to date. Returns the games added.

    This is where the app's games come from: the monthly archive is the only
    endpoint that carries live games (bullet, blitz, rapid) at all, and it also
    holds finished daily games with their final PGN and result.

    Runs in the Celery worker (`tasks.sync_user_task`), never in a request: a
    first sync reads the user's entire history, which for a multi-year account is
    dozens of sequential HTTP calls. See `_due_months` for what gets read.
    """
    client = Client(username=user.chess_username)
    return _import_months(user, client, _due_months(user, client.archive_months()))


def import_all_archives(user, months: int | None = None) -> int:
    """Force a re-read of a user's archive, ignoring what was already imported.

    The override behind `manage.py import_archives`: unlike `sync_user` it does
    not skip months that have an `ArchiveImport` row, so it is the way to repair
    a history imported by an older version or one whose rows say more than the DB
    actually holds. ``months`` caps how many of the most recent months are read;
    None means the whole history. Idempotent — a game already stored is updated
    in place.
    """
    client = Client(username=user.chess_username)
    wanted = client.archive_months()
    if months is not None:
        wanted = wanted[-months:]
    return _import_months(user, client, list(reversed(wanted)))  # newest first


def request_sync(user) -> bool:
    """Claim this user's archive sync and hand it to the worker.

    Returns True when *this* request won the claim and queued the work, which is
    also the signal the games fragment uses to re-fetch itself once, six seconds
    later, by which time the import has had a moment to land. Only `views.game_list`
    calls this: the home page starts nothing.

    The claim is a conditional UPDATE on `last_synced_at` rather than a cache
    key. One statement, atomic in Postgres and in the SQLite the tests use, so N
    web replicas racing on the same user produce exactly one sync — and if it
    ever stops working it stops loudly, where a forgotten cache backend would
    quietly fall back to per-process memory and bring back the duplicate fetches
    this design exists to remove.
    """
    if not is_linked(user):
        return False

    claimed = (
        User.objects.filter(pk=user.pk, is_active=True)
        .filter(
            Q(last_synced_at__isnull=True)
            | Q(last_synced_at__lt=timezone.now() - SYNC_COOLDOWN)
        )
        .update(last_synced_at=timezone.now())
    )
    if not claimed:
        return False

    try:
        # Imported here, not at module scope: `tasks` imports this module back
        # (see `analyze_game_task` above), so a top-level import would be a cycle.
        from ..tasks import sync_user_task

        # `retry=False`: `task_publish_retry` is on by default, so with Redis
        # down an unguarded publish would block the *home page* for seconds of
        # backoff and then raise. Nothing in a request may depend on the broker
        # being up.
        sync_user_task.apply_async(args=[user.pk], retry=False)
    except Exception:
        logger.warning(
            "Could not enqueue the archive sync for user %s", user.pk, exc_info=True
        )
        # The claim is kept, not released: one publish attempt per cooldown is
        # the right rate while the broker is down, rather than one per Sync press.
        return False
    return True


def requeue_stale_analyses(user=None) -> int:
    """Revive analyses whose worker started and never came back.

    The `CoachSuggestion` row doubles as the in-flight lock, so a RUNNING row that
    is never completed strands the position: every later `get_or_create` finds it
    and enqueues nothing, and the page goes on reporting an analysis in progress
    however often it is refreshed. This gives the lock an expiry.

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
    re-enqueued this sweep.

    ``user`` narrows the sweep to one person's rows, which is what the request
    path passes: a user's page load should fix the card they are looking at, not
    do the whole deployment's housekeeping.
    """
    cutoff = timezone.now() - ANALYSIS_TIMEOUT
    stale = CoachSuggestion.objects.filter(
        status=CoachSuggestion.Status.RUNNING, updated_at__lt=cutoff
    )
    if user is not None:
        stale = stale.filter(user=user)

    requeued = 0
    for row in stale[:REQUEUE_BATCH_SIZE]:
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


def requeue_orphaned_analyses(user=None) -> int:
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
    there are no false positives and recovery takes one sweep.

    ``user`` narrows which rows are revived, but **not** the two preconditions:
    "is anything running?" and "is the queue empty?" are facts about the whole
    deployment. Scoped to one user they would read as "empty" while another
    user's analyses were being served, and revive rows whose messages were merely
    waiting their turn.

    Deliberately conservative: it does nothing while the queue depth is unknown
    or non-zero, and no attempt is counted, since nothing was attempted. Returns
    the number of rows re-enqueued this sweep.
    """
    if CoachSuggestion.objects.filter(status=CoachSuggestion.Status.RUNNING).exists():
        return 0  # a worker is busy, so the queue is being served

    depth = _queued_task_count()
    if depth is None or depth > 0:
        return 0  # unreadable, or there really is work waiting

    orphaned = CoachSuggestion.objects.filter(status=CoachSuggestion.Status.PENDING)
    if user is not None:
        orphaned = orphaned.filter(user=user)

    requeued = 0
    for row in orphaned.order_by("updated_at")[:REQUEUE_BATCH_SIZE]:
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


def recover_stuck_analyses(user) -> int:
    """Run both recovery sweeps for `user`, at most once per `RECOVERY_INTERVAL`.

    Called from the detail page's Refresh button (`views.game_position` with
    `refresh`), which is the one request that means "show me where the analysis
    got to" — so the check runs exactly when somebody is waiting on an answer,
    and a stuck analysis is unstuck by the same press that asks after it, rather
    than waiting out a scheduler tick. Navigation deliberately does not call it.

    Never raises. This sits in front of a fragment render, and a broker that is
    down must degrade to a page that shows what the database holds, not to a 500.
    """
    global _last_recovery

    now = timezone.now()
    if _last_recovery is not None and now - _last_recovery < RECOVERY_INTERVAL:
        return 0
    _last_recovery = now

    recovered = 0
    try:
        recovered += requeue_stale_analyses(user=user)
    except Exception:
        logger.exception("Stale-analysis requeue failed")
    try:
        # After the sweep above, so a row it has just put back on the queue is not
        # mistaken for one whose message went missing.
        recovered += requeue_orphaned_analyses(user=user)
    except Exception:
        logger.exception("Orphaned-analysis requeue failed")
    return recovered
