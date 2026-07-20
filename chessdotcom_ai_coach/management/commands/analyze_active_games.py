"""Run one auto-analysis tick for active games.

Periodic scheduling is handled by Celery Beat; this command exists for manual
execution (for example ad-hoc runs and debugging).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ...services import auto_analyze


class Command(BaseCommand):
    help = "Run one auto-analysis tick for eligible active games."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-per-tick",
            type=int,
            default=getattr(settings, "AUTO_ANALYZE_MAX_PER_TICK", None),
            help="Max analyses started in this tick (default from settings).",
        )

    def handle(self, *args, **options):
        max_per_tick = options["max_per_tick"]

        if max_per_tick is not None and max_per_tick < 0:
            raise CommandError("--max-per-tick must be >= 0")
        if not getattr(settings, "AUTO_ANALYZE_ENABLED", True):
            self.stdout.write("AUTO_ANALYZE_ENABLED is off; tick skipped.")
            return
        started = auto_analyze.run_once(max_per_tick=max_per_tick)
        self.stdout.write(f"Tick complete: {started} analysis(es) started.")
