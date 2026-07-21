import os

from django.apps import AppConfig


class ChessdotcomAiCoach(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chessdotcom_ai_coach"
    verbose_name = "Chessdotcom AI Coach"

    def ready(self):
        scheduler_enabled = os.getenv("ANALYSIS_SCHEDULER_ENABLED", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        if not scheduler_enabled:
            return
        from .scheduler import start_analysis_scheduler

        start_analysis_scheduler()
