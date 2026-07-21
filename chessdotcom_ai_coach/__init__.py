# Ensure the Celery app is loaded when Django starts so @shared_task decorators
# are registered early and the app is ready before any workers pick up tasks.
from .celery import app as celery_app

__all__ = ("celery_app",)
