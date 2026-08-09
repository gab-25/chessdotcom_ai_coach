"""Celery tasks: the background executor for coach analysis.

`analyze_game_task` wraps the existing async `get_best_move` with
`async_to_sync` (Celery tasks run synchronously) and persists the result to
`CoachSuggestion` — the same outcome the view used to produce inline, but
out-of-band. `services/coach.py` is left untouched so its test mocking seam
(patching `popen_uci` / `AsyncOpenAI`) still applies.

The task also owns the row's in-flight bookkeeping: it claims the position as
RUNNING before doing any work and counts the attempt there. Counting it here
rather than where the task was enqueued is what makes the scheduler's timeout
meaningful — a task can sit queued behind a whole-game backfill for far longer
than an analysis takes, and that wait must not be mistaken for a failure.
"""

import logging

from asgiref.sync import async_to_sync
from celery import shared_task

from .models import MAX_ANALYSIS_ATTEMPTS, CoachSuggestion
from .services import board as board_utils
from .services.coach import get_best_move

logger = logging.getLogger(__name__)


def _claim(user_id: int, game_id: str, fen: str) -> CoachSuggestion | None:
    """Take ownership of the position, or return None if it must not be analysed.

    Marks the row RUNNING and counts the attempt. Returns None when there is
    nothing to do — the position is already settled (DONE/FAILED, e.g. a message
    redelivered after the analysis had in fact finished) or it has burned through
    `MAX_ANALYSIS_ATTEMPTS`, in which case it is retired as FAILED. The cap is
    enforced here and not only in the scheduler because `task_acks_late` means a
    task that kills its worker is redelivered by the broker, and something has to
    stop that loop.
    """
    row, _created = CoachSuggestion.objects.get_or_create(
        user_id=user_id,
        game_id=game_id,
        fen=fen,
        defaults={
            "status": CoachSuggestion.Status.PENDING,
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": "",
            "analysis": "",
        },
    )
    if row.status in (CoachSuggestion.Status.DONE, CoachSuggestion.Status.FAILED):
        return None

    if row.attempts >= MAX_ANALYSIS_ATTEMPTS:
        logger.warning(
            "Giving up on analysis for game %s move %s after %d attempts",
            game_id,
            row.move_no,
            row.attempts,
        )
        row.status = CoachSuggestion.Status.FAILED
        row.save()
        return None

    row.status = CoachSuggestion.Status.RUNNING
    row.attempts += 1
    # `auto_now` on updated_at: this stamps when the analysis actually started,
    # which is what `services.scheduler.requeue_stale_analyses` times out on.
    row.save()
    return row


@shared_task(name="chessdotcom_ai_coach.analyze_game_task")
def analyze_game_task(user_id: int, game_id: str, fen: str, pgn: str | None = None):
    """Run Stockfish + LLM for `fen` and persist the analysis as DONE."""
    if _claim(user_id, game_id, fen) is None:
        return

    suggestion = async_to_sync(get_best_move)(fen, pgn)

    # `get_best_move` never raises: on an engine error it returns a suggestion with
    # neither a move nor an evaluation, carrying the reason in `analysis`. That is
    # a failure, so record it as one — storing it DONE would render an "analyzed"
    # card claiming the coach recommended nothing. The eval check is what keeps a
    # *terminal* position out of this branch: there Stockfish has no move to
    # suggest but still scores the position.
    failed = not suggestion["best_move_san"] and suggestion["eval_cp"] is None

    CoachSuggestion.objects.update_or_create(
        user_id=user_id,
        game_id=game_id,
        fen=fen,
        defaults={
            "status": (
                CoachSuggestion.Status.FAILED
                if failed
                else CoachSuggestion.Status.DONE
            ),
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": suggestion["eval_text"],
            "eval_cp": suggestion["eval_cp"],
            "best_move_san": suggestion["best_move_san"],
            "best_move_uci": suggestion["best_move_uci"],
            "analysis": suggestion["analysis"],
        },
    )
