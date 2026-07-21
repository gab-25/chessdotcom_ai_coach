"""Management command: start the APScheduler auto-analysis scheduler.

Usage::

    python manage.py run_scheduler

The scheduler runs in the foreground and blocks until interrupted.  In Docker
it is launched as a dedicated ``scheduler`` service.

Environment variables that control behaviour:

``SCHEDULER_INTERVAL``
    Seconds between ticks (default ``1``).
``SCHEDULER_BATCH_SIZE``
    Maximum number of games enqueued per tick (default ``50``).
``CELERY_BROKER_URL``
    Redis URL used by Celery (default ``redis://localhost:6379/0``).
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Start the APScheduler background job that auto-enqueues analysis "
        "tasks for active chess games."
    )

    def handle(self, *args, **options):
        self.stdout.write("Starting auto-analysis scheduler …")
        # Import here so Django is fully initialised before touching the ORM.
        from chessdotcom_ai_coach.scheduler import start_scheduler

        start_scheduler()
