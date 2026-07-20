from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from .services import auto_analyze

logger = logging.getLogger(__name__)


@shared_task
def auto_analyze_active_games(max_per_tick: int | None = None) -> int:
    if not settings.AUTO_ANALYZE_ENABLED:
        logger.info("auto-analyze task skipped: AUTO_ANALYZE_ENABLED is off")
        return 0

    if max_per_tick is None:
        max_per_tick = settings.AUTO_ANALYZE_MAX_PER_TICK

    started = auto_analyze.run_once(max_per_tick=max_per_tick)
    logger.info("auto-analyze task complete: started=%s", started)
    return started
