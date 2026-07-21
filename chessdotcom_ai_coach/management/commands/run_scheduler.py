"""Run the APScheduler that polls for games due for analysis every 1 second.

This is the single source of scheduling in the project (there is no Celery Beat).
Run it as its own process/service so exactly one scheduler instance exists — an
in-process scheduler under gunicorn would start once per worker and enqueue
duplicates.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand

from ...services.scheduler import enqueue_due_analyses

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Poll active games every second and enqueue Celery analysis tasks."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler()
        scheduler.add_job(
            self._tick,
            "interval",
            seconds=1,
            max_instances=1,  # never overlap a slow tick with the next
            coalesce=True,  # collapse missed runs into one
        )
        self.stdout.write(self.style.SUCCESS("Scheduler started (1s poll)."))
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()

    def _tick(self):
        try:
            enqueue_due_analyses()
        except Exception:  # a bad tick must not kill the scheduler
            logger.exception("Scheduler tick failed")
