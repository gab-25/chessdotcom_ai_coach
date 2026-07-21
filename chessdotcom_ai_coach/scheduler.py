import atexit
import fcntl
import logging
import os
import threading
from pathlib import Path
from tempfile import gettempdir

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

from .tasks import schedule_active_game_analyses

logger = logging.getLogger(__name__)

MIN_ALLOWED_SCHEDULER_INTERVAL_SECONDS = 0.1
SCHEDULER_LOCK_PATH = Path(gettempdir()) / "chessdotcom_ai_coach_analysis_scheduler.lock"
_scheduler_lock = threading.Lock()
_scheduler_started = False
_scheduler = None


def _trigger_scheduler_tick():
    with SCHEDULER_LOCK_PATH.open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return

        try:
            schedule_active_game_analyses.delay()
        except Exception as exc:
            logger.exception(
                "analysis_scheduler_enqueue_failed",
                extra={"error_type": exc.__class__.__name__, "error": str(exc)},
            )
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def start_analysis_scheduler():
    global _scheduler, _scheduler_started

    with _scheduler_lock:
        if _scheduler_started:
            return

        try:
            interval = max(
                MIN_ALLOWED_SCHEDULER_INTERVAL_SECONDS,
                float(os.getenv("ANALYSIS_SCHEDULER_INTERVAL_SECONDS", "1")),
            )
        except ValueError:
            logger.warning(
                "analysis_scheduler_invalid_interval",
                extra={"value": os.getenv("ANALYSIS_SCHEDULER_INTERVAL_SECONDS")},
            )
            interval = 1.0
        _scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE)
        _scheduler.add_job(
            _trigger_scheduler_tick,
            "interval",
            id="schedule-active-game-analyses",
            seconds=interval,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        atexit.register(lambda: _scheduler.shutdown(wait=True))
        _scheduler_started = True
        logger.info("analysis_scheduler_started", extra={"interval_seconds": interval})
