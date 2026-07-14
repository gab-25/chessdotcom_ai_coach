"""Unit tests for the views.

The Chess.com ``Client`` and the async coach are mocked, so no network,
engine or LLM is touched. A SQLite DB (see conftest) backs auth.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        username="MyUser", password="pw12345!"
    )


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client


def _sample_game():
    return {
        "game_id": "944768131",
        "url": "https://www.chess.com/game/daily/944768131",
        "turn": "white",
        "time_class": "daily",
        "time_control": "1/86400",
        "is_my_turn": True,
        "white": {"username": "MyUser", "rating": "1500"},
        "black": {"username": "Opponent", "rating": "1600"},
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    }


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.Client")
class TestHome:
    def test_lists_games(self, mock_client, auth_client):
        mock_client.return_value.my_current_games.return_value = [_sample_game()]

        response = auth_client.get("/")

        assert response.status_code == 200
        assert list(response.context["games"]) == [_sample_game()]

    def test_renders_error_page_on_failure(self, mock_client, auth_client):
        mock_client.return_value.my_current_games.side_effect = Exception("boom")

        response = auth_client.get("/")

        assert response.status_code == 500
        assert b"Something went wrong" in response.content

    def test_requires_login(self, mock_client, client):
        response = client.get("/")

        assert response.status_code == 302
        assert "/login" in response["Location"]


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.Client")
class TestGameList:
    def test_returns_fragment_with_games(self, mock_client, auth_client):
        mock_client.return_value.my_current_games.return_value = [_sample_game()]

        response = auth_client.get("/games")

        assert response.status_code == 200
        assert b"Opponent" in response.content

    def test_degrades_silently_to_empty_on_error(self, mock_client, auth_client):
        mock_client.return_value.my_current_games.side_effect = Exception("boom")

        response = auth_client.get("/games")

        # Polling must not break the page: 200 + empty state, no 500.
        assert response.status_code == 200
        assert b"No active games" in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.Client")
class TestGameDetail:
    def test_orientation_white_when_user_is_white(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = {
            "game": _sample_game(),
            "white_name": "MyUser",
            "black_name": "Opponent",
        }

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert response.context["orientation"] == "white"

    def test_orientation_black_when_user_is_black(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = {
            "game": _sample_game(),
            "white_name": "Opponent",
            "black_name": "MyUser",
        }

        response = auth_client.get("/game/944768131")

        assert response.context["orientation"] == "black"

    def test_renders_not_found_message(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = None

        response = auth_client.get("/game/nope")

        assert response.status_code == 200
        assert b"Game not found" in response.content

    def test_renders_error_page_on_failure(self, mock_client, auth_client):
        mock_client.return_value.game_detail.side_effect = Exception("boom")

        response = auth_client.get("/game/944768131")

        assert response.status_code == 500


@pytest.mark.django_db(transaction=True)
@patch("chessdotcom_ai_coach.views.get_best_move", new_callable=AsyncMock)
@patch("chessdotcom_ai_coach.views.Client")
class TestCoachSuggestion:
    """coach_suggestion is async, so it is driven through the ASGI async_client."""

    async def test_returns_analysis_fragment(
        self, mock_client, mock_coach, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        mock_client.return_value.game_detail.return_value = {
            "game": _sample_game(),
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        mock_coach.return_value = "Play e4, a strong central move."

        response = await async_client.get("/game/944768131/coach")

        assert response.status_code == 200
        assert b"Play e4, a strong central move." in response.content

    async def test_handles_missing_game(
        self, mock_client, mock_coach, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        mock_client.return_value.game_detail.return_value = None

        response = await async_client.get("/game/nope/coach")

        assert response.status_code == 200
        assert b"Game not found" in response.content
        mock_coach.assert_not_called()


@pytest.mark.django_db
class TestLogout:
    def test_redirects_to_login(self, auth_client):
        response = auth_client.get("/logout")

        assert response.status_code == 302
        assert response["Location"] == "/login"
