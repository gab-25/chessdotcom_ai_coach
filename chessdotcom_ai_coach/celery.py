import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

app = Celery("chessdotcom_ai_coach")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

try:
    interval = float(os.getenv("ANALYSIS_SCHEDULER_INTERVAL_SECONDS", "1"))
except ValueError:
    interval = 1.0

app.conf.beat_schedule = {
    "schedule-active-game-analyses": {
        "task": "chessdotcom_ai_coach.tasks.schedule_active_game_analyses",
        "schedule": max(0.1, interval),
    }
}
