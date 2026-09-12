"""Unit tests for the Celery analysis task.

`get_best_move` is mocked (an AsyncMock, since the task bridges it via
`async_to_sync`), so no Stockfish subprocess or LLM is touched. We assert the
task persists the result as a DONE `CoachSuggestion` — and that it does the
in-flight bookkeeping around it: claiming the row as RUNNING, counting the
attempt, and retiring a position that has burned through the cap.
"""

from datetime import timedelta
from unittest.mock import ANY, AsyncMock, patch

import pytest
from django.utils import timezone

from chessdotcom_ai_coach.models import MAX_ANALYSIS_ATTEMPTS, CoachSuggestion
from chessdotcom_ai_coach.tasks import _claim, analyze_game_task

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

SUGGESTION = {
    "eval_text": "The position is balanced (+0.20).",
    "eval_cp": 0.2,
    "best_move_san": "e4",
    "best_move_uci": "e2e4",
    "analysis": "Play e4, a strong central move.",
}


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


def _pending_row(user, **overrides):
    """The row an enqueuer leaves behind for the worker to pick up."""
    defaults = {
        "status": CoachSuggestion.Status.PENDING,
        "eval_text": "",
        "analysis": "",
    }
    defaults.update(overrides)
    return CoachSuggestion.objects.create(
        user=user, game_id="944768131", fen=FEN, **defaults
    )


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.get_best_move", new_callable=AsyncMock)
class TestAnalyzeGameTask:
    def test_persists_done_suggestion(self, mock_coach, user):
        mock_coach.return_value = SUGGESTION

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        row = CoachSuggestion.objects.get(user=user, game_id="944768131", fen=FEN)
        assert row.status == CoachSuggestion.Status.DONE
        assert row.analysis == "Play e4, a strong central move."
        assert row.best_move_san == "e4"
        assert row.best_move_uci == "e2e4"
        assert row.eval_cp == 0.2
        assert row.move_no == 1

    def test_overwrites_pending_row(self, mock_coach, user):
        # The enqueue path pre-created a PENDING row; the task fills it in.
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=FEN,
            status=CoachSuggestion.Status.PENDING,
            eval_text="",
            analysis="",
        )
        mock_coach.return_value = SUGGESTION

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        assert CoachSuggestion.objects.filter(game_id="944768131").count() == 1
        row = CoachSuggestion.objects.get(game_id="944768131")
        assert row.status == CoachSuggestion.Status.DONE
        assert row.analysis == "Play e4, a strong central move."

    def test_passes_pgn_to_coach(self, mock_coach, user):
        mock_coach.return_value = SUGGESTION

        analyze_game_task(user.id, "944768131", FEN, pgn="1. e4 e5 *")

        mock_coach.assert_awaited_once_with(FEN, "1. e4 e5 *")

    def test_counts_the_attempt(self, mock_coach, user):
        """The attempt is counted here, not where the task was enqueued: a task can
        sit queued far longer than an analysis takes, and that wait must not spend a
        retry."""
        row = _pending_row(user)
        mock_coach.return_value = SUGGESTION

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.DONE
        assert row.attempts == 1

    def test_retires_a_position_past_the_attempt_cap(self, mock_coach, user):
        """`task_acks_late` means the broker redelivers a task that killed its
        worker, so the cap has to stop that loop here and not only in the
        recovery sweep."""
        row = _pending_row(user, attempts=MAX_ANALYSIS_ATTEMPTS)

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        mock_coach.assert_not_awaited()
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.FAILED

    def test_skips_a_position_already_analysed(self, mock_coach, user):
        # A message redelivered after the analysis in fact completed: don't redo it.
        row = _pending_row(user, status=CoachSuggestion.Status.DONE, best_move_san="e4")

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        mock_coach.assert_not_awaited()
        row.refresh_from_db()
        assert row.best_move_san == "e4"

    def test_records_an_engine_failure_as_failed(self, mock_coach, user):
        """`get_best_move` never raises: on an engine error it returns neither a move
        nor an eval, with the reason in `analysis`. Storing that DONE would render an
        "analyzed" card recommending nothing."""
        _pending_row(user)
        mock_coach.return_value = {
            "eval_text": "Analysis unavailable.",
            "eval_cp": None,
            "best_move_san": None,
            "best_move_uci": None,
            "analysis": "Error during Stockfish analysis: boom",
        }

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        row = CoachSuggestion.objects.get(game_id="944768131")
        assert row.status == CoachSuggestion.Status.FAILED
        assert "boom" in row.analysis

    def test_terminal_position_is_not_a_failure(self, mock_coach, user):
        # Checkmate: Stockfish has no move to suggest but still scores the position.
        _pending_row(user)
        mock_coach.return_value = {**SUGGESTION, "best_move_san": None, "best_move_uci": None}

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        assert CoachSuggestion.objects.get(game_id="944768131").status == (
            CoachSuggestion.Status.DONE
        )

    def test_creates_the_row_when_it_is_gone(self, mock_coach, user):
        # Nothing pre-created it (a manual `.delay`, or the row was deleted).
        mock_coach.return_value = SUGGESTION

        analyze_game_task(user.id, "944768131", FEN, pgn=None)

        row = CoachSuggestion.objects.get(game_id="944768131")
        assert row.status == CoachSuggestion.Status.DONE
        assert row.attempts == 1
        mock_coach.assert_awaited_once_with(FEN, ANY)


@pytest.mark.django_db
class TestClaim:
    """The hand-off itself: what the row looks like while the worker is on it, which
    is what `sync.requeue_stale_analyses` times out against."""

    def test_marks_the_row_running(self, user):
        row = _pending_row(user)

        claimed = _claim(user.id, "944768131", FEN)

        assert claimed is not None
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.RUNNING
        assert row.attempts == 1

    def test_refreshes_updated_at_as_the_start_of_the_analysis(self, user):
        row = _pending_row(user)
        CoachSuggestion.objects.filter(pk=row.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        _claim(user.id, "944768131", FEN)

        row.refresh_from_db()
        # Not the two-hour-old enqueue time: the timeout measures the analysis, so a
        # long queue wait must not make a freshly-started one look stuck.
        assert timezone.now() - row.updated_at < timedelta(minutes=1)
