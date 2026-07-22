"""Unit tests for the views.

The Chess.com ``Client`` and the async coach are mocked, so no network,
engine or LLM is touched. A SQLite DB (see conftest) backs auth.
"""

from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from chessdotcom_ai_coach.models import CoachSuggestion, Game


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
class TestHome:
    def test_lists_games(self, auth_client, user):
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            is_active=True,
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        games = list(response.context["games"])
        assert len(games) == 1
        assert games[0].game_id == "944768131"
        # The view enriches each game with the glyph board + move number.
        assert len(games[0].cells) == 64
        assert games[0].move_no == 1

    def test_does_not_call_chess_com(self, auth_client):
        # Home is a plain DB read now — the scheduler owns Chess.com syncing.
        with patch("chessdotcom_ai_coach.views.Client") as mock_client:
            response = auth_client.get("/")

        assert response.status_code == 200
        mock_client.assert_not_called()

    def test_requires_login(self, client):
        response = client.get("/")

        assert response.status_code == 302
        assert "/login" in response["Location"]


@pytest.mark.django_db
class TestGameList:
    def test_returns_fragment_with_games(self, auth_client, user):
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            is_active=True,
        )

        response = auth_client.get("/games")

        assert response.status_code == 200
        assert b"Opponent" in response.content

    def test_does_not_write_to_the_db(self, auth_client, user):
        # No upsert happens here anymore — that's the scheduler's job now.
        auth_client.get("/games")

        assert not Game.objects.filter(user=user).exists()

    def test_renders_past_games_section(self, auth_client, user):
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b"Past games" in response.content
        assert b"PastFoe" in response.content


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

    def test_falls_back_to_stored_game_when_not_current(
        self, mock_client, auth_client, user
    ):
        # Game is no longer current on Chess.com...
        mock_client.return_value.game_detail.return_value = None
        # ...but we have a persisted snapshot.
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            pgn='[Event "T"]\n\n1. e4 e5 *',
            fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            is_active=False,
        )

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        # Past game: read-only, no re-analysis, but the moves are shown.
        assert response.context["can_analyze"] is False
        assert b"e4" in response.content
        assert b"Re-analyze" not in response.content
        assert b"Request suggestion" not in response.content

    def test_poll_swaps_in_completed_analysis(self, mock_client, auth_client, user):
        # Regression: the poll cursor tracks (FEN + analysis state), so a
        # PENDING→DONE transition on the *same* position swaps in the result
        # instead of leaving the button stuck on "Analyzing…".
        sample = _sample_game()
        mock_client.return_value.game_detail.return_value = {
            "game": sample,
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=sample["fen"],
            status=CoachSuggestion.Status.DONE,
            eval_text="Balanced (+0.20).",
            eval_cp=0.2,
            best_move_san="e4",
            best_move_uci="e2e4",
            analysis="Play e4.",
        )

        # The poller still reflects the stale PENDING token for this position.
        response = auth_client.get(
            "/game/944768131",
            {"since": f"{sample['fen']}|pending"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert b"Play e4." in response.content

    def test_poll_noop_when_analysis_state_unchanged(
        self, mock_client, auth_client, user
    ):
        sample = _sample_game()
        mock_client.return_value.game_detail.return_value = {
            "game": sample,
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=sample["fen"],
            status=CoachSuggestion.Status.DONE,
            analysis="Play e4.",
        )

        # Poller already reflects the current (DONE) token -> no swap.
        response = auth_client.get(
            "/game/944768131",
            {"since": f"{sample['fen']}|done"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 204


@pytest.mark.django_db(transaction=True)
@patch("chessdotcom_ai_coach.views.analyze_game_task")
@patch("chessdotcom_ai_coach.views.Client")
class TestCoachSuggestion:
    """coach_suggestion is async, so it is driven through the ASGI async_client.

    Analysis now runs in the background: the view enqueues a Celery task (mocked
    here) and renders the stored/queued state instead of computing inline.
    """

    async def test_enqueues_task_and_shows_pending(
        self, mock_client, mock_task, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        mock_client.return_value.game_detail.return_value = {
            "game": _sample_game(),
            "white_name": "MyUser",
            "black_name": "Opponent",
        }

        response = await async_client.get("/game/944768131/coach")

        assert response.status_code == 200
        assert b"Analyzing in background" in response.content
        # The button is blocked while analysis runs in the background.
        assert b"disabled" in response.content
        # A pending row is created and exactly one task is enqueued.
        mock_task.delay.assert_called_once()
        row = await sync_to_async(
            CoachSuggestion.objects.get
        )(user=user, game_id="944768131")
        assert row.status == CoachSuggestion.Status.PENDING

    async def test_reanalyze_reenqueues_finished_position(
        self, mock_client, mock_task, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        sample = _sample_game()
        mock_client.return_value.game_detail.return_value = {
            "game": sample,
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        # A finished analysis already exists for the current position.
        await sync_to_async(CoachSuggestion.objects.create)(
            user=user,
            game_id="944768131",
            fen=sample["fen"],
            status=CoachSuggestion.Status.DONE,
            eval_text="The position is balanced (+0.20).",
            eval_cp=0.2,
            best_move_san="e4",
            best_move_uci="e2e4",
            analysis="Play e4, a strong central move.",
        )

        response = await async_client.get("/game/944768131/coach")

        # Re-analyze re-enqueues the task and resets the row to PENDING, clearing
        # the stale result so the panel shows the "Analyzing…" state again.
        assert response.status_code == 200
        assert b"Analyzing in background" in response.content
        assert b"Play e4, a strong central move." not in response.content
        assert b"disabled" in response.content  # button blocked during re-analysis
        mock_task.delay.assert_called_once()
        row = await sync_to_async(
            CoachSuggestion.objects.get
        )(user=user, game_id="944768131")
        assert row.status == CoachSuggestion.Status.PENDING
        assert row.analysis == ""
        assert row.best_move_san is None

    async def test_handles_missing_game(
        self, mock_client, mock_task, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        mock_client.return_value.game_detail.return_value = None

        response = await async_client.get("/game/nope/coach")

        assert response.status_code == 200
        assert b"Game not found" in response.content
        mock_task.delay.assert_not_called()
        assert await sync_to_async(CoachSuggestion.objects.count)() == 0

    async def test_same_position_does_not_reenqueue(
        self, mock_client, mock_task, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        mock_client.return_value.game_detail.return_value = {
            "game": _sample_game(),
            "white_name": "MyUser",
            "black_name": "Opponent",
        }

        await async_client.get("/game/944768131/coach")
        await async_client.get("/game/944768131/coach")  # same FEN → dedup

        assert mock_task.delay.call_count == 1
        count = await sync_to_async(
            CoachSuggestion.objects.filter(user=user, game_id="944768131").count
        )()
        assert count == 1

    async def test_new_position_enqueues_again(
        self, mock_client, mock_task, async_client, user
    ):
        await sync_to_async(async_client.force_login)(user)
        first = {
            "game": _sample_game(),
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        advanced = _sample_game()
        advanced["fen"] = "rnbqkbnr/pppp1ppp/8/4p3/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 2"
        second = {
            "game": advanced,
            "white_name": "MyUser",
            "black_name": "Opponent",
        }
        mock_client.return_value.game_detail.side_effect = [first, second]

        await async_client.get("/game/944768131/coach")
        await async_client.get("/game/944768131/coach")  # different FEN → new row

        assert mock_task.delay.call_count == 2
        count = await sync_to_async(
            CoachSuggestion.objects.filter(user=user, game_id="944768131").count
        )()
        assert count == 2


@pytest.mark.django_db
class TestLogout:
    def test_redirects_to_login(self, auth_client):
        response = auth_client.get("/logout")

        assert response.status_code == 302
        assert response["Location"] == "/login"
