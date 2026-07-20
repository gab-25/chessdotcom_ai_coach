"""Unit tests for the auto-analysis worker service.

The Chess.com ``Client`` and the async coach (``get_best_move``) are mocked, so
no network, engine or LLM is touched. A SQLite DB (see conftest) backs the ORM.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services import auto_analyze

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN2 = "8/8/8/8/8/8/8/K6k w - - 0 1"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


def _game(game_id="1", *, is_my_turn=True, fen=FEN, **over):
    game = {
        "game_id": game_id,
        "url": f"https://www.chess.com/game/daily/{game_id}",
        "time_class": "daily",
        "is_my_turn": is_my_turn,
        "white": {"username": "MyUser", "rating": "1500"},
        "black": {"username": "Opponent", "rating": "1600"},
        "pgn": "1. e4 e5",
        "fen": fen,
    }
    game.update(over)
    return game


def _suggestion():
    return {
        "eval_text": "The position is balanced (+0.10).",
        "eval_cp": 0.1,
        "best_move_san": "Nf3",
        "best_move_uci": "g1f3",
        "analysis": "Solid developing move.",
    }


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.auto_analyze.get_best_move", new_callable=AsyncMock)
@patch("chessdotcom_ai_coach.services.auto_analyze.Client")
class TestRunOnce:
    """``run_once`` scans active-game users and starts eligible analyses.

    Decorator order: the innermost ``@patch`` (``Client``) is the first arg
    after ``self``, then ``get_best_move``.
    """

    def _make_active(self, user, game_id="1"):
        """A user is only scanned if it has an active Game row."""
        Game.objects.create(user=user, game_id=game_id, is_active=True)

    def test_starts_analysis_for_eligible_game(self, mock_client, mock_best, user):
        self._make_active(user)
        mock_client.return_value.my_current_games.return_value = [_game("1")]
        mock_best.return_value = _suggestion()

        started = auto_analyze.run_once()

        assert started == 1
        row = CoachSuggestion.objects.get(user=user, game_id="1", fen=FEN)
        assert row.best_move_san == "Nf3"
        assert row.best_move_uci == "g1f3"

    def test_skips_when_not_users_turn(self, mock_client, mock_best, user):
        self._make_active(user)
        mock_client.return_value.my_current_games.return_value = [
            _game("1", is_my_turn=False)
        ]

        started = auto_analyze.run_once()

        assert started == 0
        mock_best.assert_not_awaited()
        assert not CoachSuggestion.objects.filter(user=user).exists()

    def test_idempotent_when_already_analyzed(self, mock_client, mock_best, user):
        self._make_active(user)
        CoachSuggestion.objects.create(
            user=user, game_id="1", fen=FEN, eval_text="x", analysis="existing"
        )
        mock_client.return_value.my_current_games.return_value = [_game("1")]

        started = auto_analyze.run_once()

        assert started == 0
        mock_best.assert_not_awaited()
        # The pre-existing analysis is left untouched (no overwrite).
        assert CoachSuggestion.objects.get(user=user, game_id="1").analysis == "existing"

    def test_respects_max_per_tick(self, mock_client, mock_best, user):
        self._make_active(user)
        mock_client.return_value.my_current_games.return_value = [
            _game("1", fen=FEN),
            _game("2", fen=FEN2),
        ]
        mock_best.return_value = _suggestion()

        started = auto_analyze.run_once(max_per_tick=1)

        assert started == 1
        assert CoachSuggestion.objects.filter(user=user).count() == 1

    def test_upserts_current_games(self, mock_client, mock_best, user):
        self._make_active(user)
        mock_client.return_value.my_current_games.return_value = [_game("1")]
        mock_best.return_value = _suggestion()

        auto_analyze.run_once()

        game = Game.objects.get(user=user, game_id="1")
        assert game.white_name == "MyUser"
        assert game.is_active is True

    def test_skips_users_without_active_games(self, mock_client, mock_best, user):
        # No active Game row -> user is not scanned at all.
        started = auto_analyze.run_once()

        assert started == 0
        mock_client.assert_not_called()
