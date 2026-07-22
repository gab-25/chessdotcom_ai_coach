"""Run the APScheduler that syncs games from Chess.com and enqueues analysis.

This is the single source of scheduling in the project (there is no Celery Beat).
Run it as its own process/service so exactly one scheduler instance exists — an
in-process scheduler under gunicorn would start once per worker and enqueue
duplicates.

Every tick (5s — matching the home page's own HTMX poll cadence, so nothing
needs data fresher than that) first syncs each linked user's current games
from Chess.com into the local DB (`sync_current_games`), then checks that DB
for games due for analysis and enqueues them (`enqueue_due_analyses`). The two
steps run in separate try/except blocks so a Chess.com outage doesn't stop the
local enqueue check from still running against whatever `Game` rows exist.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand

from ...services.scheduler import enqueue_due_analyses, sync_current_games

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


class Command(BaseCommand):
    help = "Every 5s, sync games from Chess.com and enqueue Celery analysis tasks."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler()
        scheduler.add_job(
            self._tick,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            max_instances=1,  # never overlap a slow tick with the next
            coalesce=True,  # collapse missed runs into one
        )
        self.stdout.write(
            self.style.SUCCESS(f"Scheduler started ({POLL_INTERVAL_SECONDS}s poll).")
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
            enqueue_due_analyses()
        except Exception:
            logger.exception("Scheduler tick failed")
