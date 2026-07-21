"""Celery tasks for background analysis of chess games.

Each task corresponds to a single analysis run for one (game, FEN) pair.
Tasks are dispatched by the APScheduler job in :mod:`chessdotcom_ai_coach.scheduler`
and run inside Celery worker processes.
"""

import asyncio
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=5, name="chessdotcom_ai_coach.tasks.analyze_game")
def analyze_game(self, game_id: str, fen: str, user_id: int) -> None:
    """Run Stockfish + LLM analysis for *game_id* at position *fen*.

    The task is idempotent: if a :class:`~chessdotcom_ai_coach.models.CoachSuggestion`
    already exists for ``(user_id, game_id, fen)`` the work is skipped.  On
    transient errors (engine startup failures, Ollama timeouts) the task retries
    up to 3 times with a 5-second delay.

    Args:
        game_id: The Chess.com game identifier.
        fen:     The board position to analyse (FEN string).
        user_id: Primary key of the :class:`~chessdotcom_ai_coach.models.User`.
    """
    # Import here to avoid issues with Django apps not being ready at module load.
    from chessdotcom_ai_coach.models import CoachSuggestion, Game
    from chessdotcom_ai_coach.services import board as board_utils
    from chessdotcom_ai_coach.services.coach import get_best_move

    # Skip if we have already stored analysis for this exact position.
    if CoachSuggestion.objects.filter(user_id=user_id, game_id=game_id, fen=fen).exists():
        logger.debug(
            "Analysis already present for game=%s fen=%s user=%s – skipping",
            game_id, fen, user_id,
        )
        return

    try:
        game = Game.objects.get(game_id=game_id, user_id=user_id)
    except Game.DoesNotExist:
        logger.warning("Game %s for user %s not found – task aborted", game_id, user_id)
        return

    pgn = game.pgn

    try:
        suggestion = asyncio.run(get_best_move(fen, pgn))
    except Exception as exc:
        logger.error(
            "Analysis failed for game=%s fen=%s user=%s: %s",
            game_id, fen, user_id, exc,
        )
        raise self.retry(exc=exc)

    CoachSuggestion.objects.update_or_create(
        user_id=user_id,
        game_id=game_id,
        fen=fen,
        defaults={
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": suggestion["eval_text"],
            "eval_cp": suggestion["eval_cp"],
            "best_move_san": suggestion["best_move_san"],
            "best_move_uci": suggestion["best_move_uci"],
            "analysis": suggestion["analysis"],
        },
    )
    logger.info(
        "Analysis stored for game=%s fen=%s user=%s move=%s",
        game_id, fen, user_id, suggestion.get("best_move_san"),
    )
