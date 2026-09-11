"""Run the APScheduler that gathers games from Chess.com and unsticks analyses.

This is the single source of scheduling in the project (there is no Celery Beat).
Run it as its own process/service so exactly one scheduler instance exists — an
in-process scheduler under gunicorn would start once per worker and duplicate
every archive fetch.

One job, on a slow cadence, because nothing it does needs to be fresh by the
second:

* `import_archive_month` — one month of each linked user's archive per run. This
  is where the app's games come from, live and daily alike.
* `sync_current_games` — the daily-only, in-progress-only endpoint, used to flip
  a daily game to finished as soon as it ends rather than waiting for the next
  archive read.
* `requeue_stale_analyses` / `requeue_orphaned_analyses` — revive analyses whose
  worker, or whose broker message, never came back.

**No job enqueues analysis.** A full archive is thousands of games at dozens of
analyses each, so a game is analysed when the user asks for it, from the detail
page or `manage.py analyze_game`.

The steps run in separate try/except blocks so a Chess.com outage doesn't stop
the local recovery checks from running against whatever `Game` rows exist.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand

from ...services.scheduler import (
    import_archive_month,
    requeue_orphaned_analyses,
    requeue_stale_analyses,
    sync_current_games,
)

logger = logging.getLogger(__name__)

TICK_INTERVAL_MINUTES = 10


class Command(BaseCommand):
    help = (
        "Every 10min, import a month of each linked user's Chess.com archive, "
        "mark finished daily games, and revive stuck analyses."
    )

    def handle(self, *args, **options):
        scheduler = BlockingScheduler()
        scheduler.add_job(
            self._tick,
            "interval",
            minutes=TICK_INTERVAL_MINUTES,
            max_instances=1,  # never overlap a slow tick with the next
            coalesce=True,  # collapse missed runs into one
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduler started ({TICK_INTERVAL_MINUTES}min tick)."
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
            # After the sync: a daily game that just ended is already is_active
            # False, so this run's read of the current month picks it up complete.
            import_archive_month()
        except Exception:
            logger.exception("Archive import failed")
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
