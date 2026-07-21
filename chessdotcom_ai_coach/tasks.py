"""Celery tasks: the background executor for coach analysis.

`analyze_game_task` wraps the existing async `get_best_move` with
`async_to_sync` (Celery tasks run synchronously) and persists the result to
`CoachSuggestion` — the same outcome the view used to produce inline, but
out-of-band. `services/coach.py` is left untouched so its test mocking seam
(patching `popen_uci` / `ollama.AsyncClient`) still applies.
"""

from asgiref.sync import async_to_sync
from celery import shared_task

from .models import CoachSuggestion
from .services import board as board_utils
from .services.coach import get_best_move


@shared_task(name="chessdotcom_ai_coach.analyze_game_task")
def analyze_game_task(user_id: int, game_id: str, fen: str, pgn: str | None = None):
    """Run Stockfish + LLM for `fen` and persist the analysis as DONE."""
    suggestion = async_to_sync(get_best_move)(fen, pgn)

    CoachSuggestion.objects.update_or_create(
        user_id=user_id,
        game_id=game_id,
        fen=fen,
        defaults={
            "status": CoachSuggestion.Status.DONE,
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": suggestion["eval_text"],
            "eval_cp": suggestion["eval_cp"],
            "best_move_san": suggestion["best_move_san"],
            "best_move_uci": suggestion["best_move_uci"],
            "analysis": suggestion["analysis"],
        },
    )
