"""Celery application for the project.

Celery is used purely as the worker/executor for background work:
`analyze_game_task` for coach analysis, `sync_user_task` for the Chess.com
archive import. **Nothing is scheduled** — there is no Celery Beat and no
scheduler process of any kind. Both tasks are enqueued from the request path (see
`services.sync`), which is what lets the web container run as many replicas as it
likes without duplicating any background work.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

app = Celery("chessdotcom_ai_coach")
# Pull CELERY_* settings from Django's config; discover tasks.py in each app.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
