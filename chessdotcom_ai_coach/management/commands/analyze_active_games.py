"""Background worker: auto-start coach analysis for active games.

Runs a simple loop — one ``auto_analyze.run_once()`` tick per ``--interval``
seconds — until interrupted. There is no queue/broker to integrate with (the
project uses none), so this command *is* the scheduler; run it as its own
process/container (see the ``worker`` service in ``docker-compose.yaml``).

Each analysis blocks the loop for as long as it takes (~2s Stockfish plus
~20-30s local LLM inference), so the interval is the *minimum* gap between
ticks, not a hard guarantee of one tick per second.
"""

from __future__ import annotations

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from ...services import auto_analyze


class Command(BaseCommand):
    help = "Continuously auto-start coach analysis for eligible active games."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=float,
            default=getattr(settings, "AUTO_ANALYZE_INTERVAL", 1.0),
            help="Seconds to wait between scan ticks (default: 1.0).",
        )
        parser.add_argument(
            "--max-per-tick",
            type=int,
            default=getattr(settings, "AUTO_ANALYZE_MAX_PER_TICK", None),
            help="Max analyses started per tick (default: unlimited).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single tick and exit (useful for cron or testing).",
        )

    def handle(self, *args, **options):
        interval = options["interval"]
        max_per_tick = options["max_per_tick"]

        if options["once"]:
            started = auto_analyze.run_once(max_per_tick=max_per_tick)
            self.stdout.write(f"Tick complete: {started} analysis(es) started.")
            return

        if not getattr(settings, "AUTO_ANALYZE_ENABLED", True):
            self.stdout.write("AUTO_ANALYZE_ENABLED is off; worker not started.")
            return

        self._running = True

        def _stop(signum, _frame):
            self.stdout.write(f"Received signal {signum}, shutting down…")
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(
            f"auto-analyze worker started (interval={interval}s, "
            f"max_per_tick={max_per_tick})."
        )
        while self._running:
            auto_analyze.run_once(max_per_tick=max_per_tick)
            # Sleep in short slices so a stop signal is honored promptly even
            # when the interval is large.
            slept = 0.0
            while self._running and slept < interval:
                step = min(0.5, interval - slept)
                time.sleep(step)
                slept += step

        self.stdout.write("auto-analyze worker stopped.")
