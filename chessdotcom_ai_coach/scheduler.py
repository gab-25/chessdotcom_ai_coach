"""APScheduler-based background scheduler for auto-analysis of active games.

The scheduler runs inside its own process (started via the ``run_scheduler``
management command).  Every ``SCHEDULER_INTERVAL`` seconds (default: 1) the
:func:`check_active_games_for_analysis` job:

1. Queries all active :class:`~chessdotcom_ai_coach.models.Game` rows whose
   current FEN has **not** yet been submitted for analysis.
2. Atomically "claims" each eligible game by setting
   ``analysis_enqueued_fen`` to the current FEN via a filtered ``UPDATE``.
3. Enqueues an :func:`~chessdotcom_ai_coach.tasks.analyze_game` Celery task
   for every claimed game.

Idempotency is guaranteed at the DB level: the ``UPDATE … WHERE
analysis_enqueued_fen != fen`` will match a row at most once per FEN value,
so duplicate enqueues are impossible even if multiple scheduler processes run
concurrently.

Configuration (environment variables):
    SCHEDULER_INTERVAL  Seconds between ticks (default: ``1``).
    SCHEDULER_BATCH_SIZE  Maximum games enqueued per tick (default: ``50``).
    SCHEDULER_MAX_JITTER  Random jitter added to the interval in seconds
                          (default: ``0``).
"""

import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "1"))
_BATCH_SIZE = int(os.getenv("SCHEDULER_BATCH_SIZE", "50"))
_MAX_JITTER = float(os.getenv("SCHEDULER_MAX_JITTER", "0"))


def check_active_games_for_analysis() -> None:
    """Check active games and enqueue analysis for any whose FEN has changed.

    Called every ``SCHEDULER_INTERVAL`` seconds.  The function is intentionally
    synchronous and blocks for the duration of the DB query + Celery enqueue so
    APScheduler can enforce the interval correctly.
    """
    # Deferred import: Django must be fully set up before we touch the ORM.
    from chessdotcom_ai_coach.models import Game
    from chessdotcom_ai_coach.tasks import analyze_game

    try:
        # Select active games that have a FEN and whose current FEN differs
        # from the last enqueued FEN (or was never enqueued: default is '').
        eligible = (
            Game.objects.filter(is_active=True)
            .exclude(fen="")
            .exclude(analysis_enqueued_fen=models_F("fen"))
            .select_related("user")
            [:_BATCH_SIZE]
        )
        # Materialise the queryset before the UPDATE so we can iterate.
        games = list(eligible)

        enqueued_count = 0
        for game in games:
            # Atomic claim: update the row only if another process hasn't
            # already claimed it since we read it.
            claimed = (
                Game.objects.filter(pk=game.pk)
                .exclude(analysis_enqueued_fen=game.fen)
                .update(analysis_enqueued_fen=game.fen)
            )
            if not claimed:
                # Another scheduler instance already claimed this game.
                continue

            analyze_game.delay(game.game_id, game.fen, game.user_id)
            enqueued_count += 1
            logger.info(
                "Enqueued analysis task: game=%s fen=%s user=%s",
                game.game_id, game.fen, game.user_id,
            )

        if enqueued_count:
            logger.info("Scheduler tick: enqueued %d analysis task(s)", enqueued_count)
        else:
            logger.debug("Scheduler tick: no eligible games found")

    except Exception:
        logger.exception("Scheduler tick failed unexpectedly")


# ---------------------------------------------------------------------------
# Lazy import for Django's F() expression – resolved when the module is used.
# ---------------------------------------------------------------------------

def _lazy_import_f():
    """Return Django's ``F`` expression class, importing it on first call."""
    from django.db.models import F  # noqa: PLC0415
    return F


# Overwrite module-level placeholder once Django is available.
class _LazyF:
    """Proxy that behaves like ``django.db.models.F`` after Django setup."""

    def __call__(self, *args, **kwargs):
        from django.db.models import F
        return F(*args, **kwargs)


models_F = _LazyF()


def start_scheduler() -> None:
    """Create and start a blocking APScheduler with the analysis job.

    This function blocks until the process is interrupted (SIGINT / SIGTERM).
    It is called by the ``run_scheduler`` management command.
    """
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        check_active_games_for_analysis,
        trigger=IntervalTrigger(seconds=_INTERVAL, jitter=_MAX_JITTER or None),
        id="check_active_games",
        name="Check active games for analysis",
        max_instances=1,  # prevent overlap if a tick takes longer than the interval
        coalesce=True,    # merge missed firings into a single run
    )
    logger.info(
        "Starting APScheduler: interval=%ds batch_size=%d",
        _INTERVAL, _BATCH_SIZE,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
