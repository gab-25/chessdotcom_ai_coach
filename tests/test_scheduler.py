"""Unit tests for the scheduler tick body: `sync_current_games` (Chess.com ->
DB) and `enqueue_due_analyses` (DB -> Celery).

The Celery task and the Chess.com `Client` are mocked, so no broker, worker or
network is needed.
"""

from unittest.mock import MagicMock, patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services.scheduler import (
    enqueue_due_analyses,
    sync_current_games,
)

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


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.game_store.upsert_current_games")
@patch("chessdotcom_ai_coach.services.scheduler.Client")
class TestSyncCurrentGames:
    def test_syncs_user_with_linked_chess_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="login_name",
            password="pw12345!",
            chessdotcom_username="ChessHandle",
        )
        mock_client_cls.return_value.my_current_games.return_value = ["game-dict"]

        sync_current_games()

        mock_client_cls.assert_called_once_with(username="ChessHandle")
        mock_upsert.assert_called_once_with(user, ["game-dict"])

    def test_skips_user_without_linked_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        # No chessdotcom_username set: chess_username would fall back to the
        # login username, but this user is intentionally not synced.
        django_user_model.objects.create_user(username="login_name", password="pw12345!")

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_skips_user_with_blank_linked_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username=""
        )

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_skips_inactive_user(self, mock_client_cls, mock_upsert, django_user_model):
        django_user_model.objects.create_user(
            username="login_name",
            password="pw12345!",
            chessdotcom_username="ChessHandle",
            is_active=False,
        )

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_one_users_failure_does_not_block_the_rest(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        django_user_model.objects.create_user(
            username="bad_login", password="pw12345!", chessdotcom_username="Bad"
        )
        good_user = django_user_model.objects.create_user(
            username="good_login", password="pw12345!", chessdotcom_username="Good"
        )

        def _client_for(username):
            client = MagicMock()
            if username == "Bad":
                client.my_current_games.side_effect = Exception("boom")
            else:
                client.my_current_games.return_value = ["ok"]
            return client

        mock_client_cls.side_effect = _client_for

        sync_current_games()  # must not raise

        mock_upsert.assert_called_once_with(good_user, ["ok"])
