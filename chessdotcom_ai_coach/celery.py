"""Celery application for chessdotcom_ai_coach.

Workers process analysis tasks dispatched by the APScheduler job.
The broker and result-backend URLs are read from environment variables so
the same image can be used for both the web and worker containers.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

app = Celery("chessdotcom_ai_coach")

# Read Celery config from Django settings keys prefixed with CELERY_.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks defined in each installed app's tasks.py module.
app.autodiscover_tasks()
