"""Unit tests for the scheduler poll body (`enqueue_due_analyses`).

The Celery task is mocked, so no broker or worker is needed: we assert only which
games get enqueued and that dedup prevents re-enqueuing a queued position.
"""

from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services.scheduler import enqueue_due_analyses

# White to move (FEN field 2 = "w") vs. black to move.
WHITE_TO_MOVE = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BLACK_TO_MOVE = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


def _game(user, **kwargs):
    defaults = {
        "game_id": "944768131",
        "white_name": "MyUser",
        "black_name": "Opponent",
        "fen": WHITE_TO_MOVE,
        "is_active": True,
    }
    defaults.update(kwargs)
    return Game.objects.create(user=user, **defaults)


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.analyze_game_task")
class TestEnqueueDueAnalyses:
    def test_enqueues_when_user_to_move(self, mock_task, user):
        # User plays White, White to move → enqueue.
        _game(user, fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 1
        mock_task.delay.assert_called_once()
        row = CoachSuggestion.objects.get(user=user, game_id="944768131")
        assert row.status == CoachSuggestion.Status.PENDING

    def test_skips_when_opponent_to_move(self, mock_task, user):
        # User plays White, but it's Black to move → skip.
        _game(user, fen=BLACK_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()
        assert CoachSuggestion.objects.count() == 0

    def test_skips_inactive_games(self, mock_task, user):
        _game(user, is_active=False, fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_skips_games_without_fen(self, mock_task, user):
        _game(user, fen="")

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_skips_when_user_not_a_player(self, mock_task, user):
        # Neither player matches the user's chess username.
        _game(user, white_name="Foo", black_name="Bar", fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_dedup_across_ticks(self, mock_task, user):
        # Same position on two consecutive ticks → enqueued only once.
        _game(user, fen=WHITE_TO_MOVE)

        first = enqueue_due_analyses()
        second = enqueue_due_analyses()

        assert first == 1
        assert second == 0
        assert mock_task.delay.call_count == 1
        assert CoachSuggestion.objects.filter(game_id="944768131").count() == 1

    def test_user_playing_black_to_move(self, mock_task, user):
        # User plays Black and it's Black to move → enqueue.
        _game(user, white_name="Opponent", black_name="MyUser", fen=BLACK_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 1
        mock_task.delay.assert_called_once()
