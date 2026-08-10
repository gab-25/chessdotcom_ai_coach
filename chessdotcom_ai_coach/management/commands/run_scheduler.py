"""Run the APScheduler that syncs games from Chess.com and enqueues analysis.

This is the single source of scheduling in the project (there is no Celery Beat).
Run it as its own process/service so exactly one scheduler instance exists — an
in-process scheduler under gunicorn would start once per worker and enqueue
duplicates.

Two jobs run at very different cadences, because they answer to different clocks:

* every 5s — the live tick, matching the detail page's own HTMX poll, so nothing
  needs data fresher than that. It syncs each linked user's current games from
  Chess.com into the local DB (`sync_current_games`), resolves the outcome of
  games that just ended from the archives (`backfill_results`), enqueues the
  analyses due on the active games (`enqueue_due_analyses` — both the move you're
  about to play and any earlier move the poll skipped), and revives analyses whose
  worker never came back (`requeue_stale_analyses`).
* every 10 minutes — the reconciliation scan over finished games
  (`enqueue_finished_game_analyses`). It reads the whole stored history, and a
  finished game changes only when something else failed, so there is nothing to
  gain from running it on the live tick.

Within each job the steps run in separate try/except blocks so a Chess.com outage
doesn't stop the local enqueue checks from still running against whatever `Game`
rows exist.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand

from ...services.scheduler import (
    backfill_results,
    enqueue_due_analyses,
    enqueue_finished_game_analyses,
    requeue_orphaned_analyses,
    requeue_stale_analyses,
    sync_current_games,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
FINISHED_SCAN_MINUTES = 10


class Command(BaseCommand):
    help = (
        "Every 5s, sync games from Chess.com, backfill finished-game results and "
        "enqueue Celery analysis tasks; every 10min, scan finished games for "
        "moves that were never analysed."
    )

    def handle(self, *args, **options):
        scheduler = BlockingScheduler()
        scheduler.add_job(
            self._tick,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            max_instances=1,  # never overlap a slow tick with the next
            coalesce=True,  # collapse missed runs into one
        )
        scheduler.add_job(
            self._scan_finished,
            "interval",
            minutes=FINISHED_SCAN_MINUTES,
            max_instances=1,
            coalesce=True,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduler started ({POLL_INTERVAL_SECONDS}s poll, "
                f"{FINISHED_SCAN_MINUTES}min finished-game scan)."
            )
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()

    def _tick(self):
        try:
            sync_current_games()
        except Exception:  # a bad tick must not kill the scheduler
            logger.exception("Chess.com sync failed")
        try:
            # After the sync: games that just ended are now is_active=False, so
            # resolve their outcome from the archives.
            backfill_results()
        except Exception:
            logger.exception("Result backfill failed")
        try:
            enqueue_due_analyses()
        except Exception:
            logger.exception("Scheduler tick failed")
        try:
            # Anything still RUNNING well past the analysis timeout lost its
            # worker, so hand it back to the queue (or retire it) rather than leave
            # the row locking the position for ever.
            requeue_stale_analyses()
        except Exception:
            logger.exception("Stale-analysis requeue failed")
        try:
            # Last, the other half of that: a row that lost its *message* rather
            # than its worker. Runs after the requeue above so a row it just put
            # back on the queue is not mistaken for an orphan.
            requeue_orphaned_analyses()
        except Exception:
            logger.exception("Orphaned-analysis requeue failed")

    def _scan_finished(self):
        try:
            enqueue_finished_game_analyses()
        except Exception:
            logger.exception("Finished-game analysis scan failed")
