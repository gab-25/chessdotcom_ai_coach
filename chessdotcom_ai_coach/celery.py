"""Celery application for the project.

Celery is used purely as the worker/executor for background analysis
(`analyze_game_task`). Scheduling is owned entirely by APScheduler (see
`management/commands/run_scheduler.py`) — there is no Celery Beat.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

app = Celery("chessdotcom_ai_coach")
# Pull CELERY_* settings from Django's config; discover tasks.py in each app.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
