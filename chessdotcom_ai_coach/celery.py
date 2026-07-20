from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

app = Celery("chessdotcom_ai_coach")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
