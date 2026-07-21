"""Unit tests for the Celery analysis task.

`get_best_move` is mocked (an AsyncMock, since the task bridges it via
`async_to_sync`), so no Stockfish subprocess or LLM is touched. We assert the
task persists the result as a DONE `CoachSuggestion`.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion
from chessdotcom_ai_coach.tasks import analyze_game_task

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
        # The scheduler pre-created a PENDING row; the task fills it in.
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
