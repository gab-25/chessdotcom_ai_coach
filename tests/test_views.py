"""Unit tests for the views.

The Chess.com ``Client`` and the async coach are mocked, so no network,
engine or LLM is touched. A SQLite DB (see conftest) backs auth.
"""

import json
from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game

FEN_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
PGN = '[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 *'


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        username="MyUser", password="pw12345!"
    )


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client


def _sample_game(**overrides):
    game = {
        "fen": FEN_START,
        "pgn": PGN,
        "time_class": "rapid",
        "url": "https://www.chess.com/game/live/944768131",
    }
    game.update(overrides)
    return {
        "game": game,
        "white_name": "MyUser",
        "black_name": "Opponent",
        "white_rating": "1500",
        "black_rating": "1600",
    }


def _review_data(response):
    """Pull the JSON model the view embedded via ``json_script`` for the client."""
    marker = b'<script id="gr-data" type="application/json">'
    body = response.content
    start = body.index(marker) + len(marker)
    end = body.index(b"</script>", start)
    return json.loads(body[start:end].decode())


@pytest.mark.django_db
class TestHome:
    def test_lists_games(self, auth_client, user):
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            fen=FEN_START,
            is_active=True,
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        games = list(response.context["games"])
        assert len(games) == 1
        assert games[0].game_id == "944768131"
        assert len(games[0].cells) == 64
        assert games[0].move_no == 1

    def test_does_not_call_chess_com(self, auth_client):
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
            fen=FEN_START,
            is_active=True,
        )

        response = auth_client.get("/games")

        assert response.status_code == 200
        assert b"Opponent" in response.content

    def test_does_not_write_to_the_db(self, auth_client, user):
        auth_client.get("/games")

        assert not Game.objects.filter(user=user).exists()

    def test_renders_past_games_section(self, auth_client, user):
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b"Past games" in response.content
        assert b"PastFoe" in response.content

    def test_past_games_link_to_detail(self, auth_client, user):
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b'href="/game/old1"' in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.Client")
class TestGameDetail:
    """The unified detail page: same UI for live and finished games."""

    def test_live_game_renders_and_is_live(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = _sample_game()

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert response.context["is_live"] is True
        assert response.context["orientation"] == "white"
        assert b'id="gr-board"' in response.content
        data = _review_data(response)
        assert data["meta"]["isLive"] is True
        assert [p["san"] for p in data["plies"]] == ["e4", "e5", "Nf3", "Nc6"]
        assert len(data["positions"]) == len(data["plies"]) + 1

    def test_orientation_black_when_user_is_black(self, mock_client, auth_client):
        game = _sample_game()
        game["white_name"], game["black_name"] = "Opponent", "MyUser"
        mock_client.return_value.game_detail.return_value = game

        response = auth_client.get("/game/944768131")

        assert response.context["orientation"] == "black"
        assert _review_data(response)["meta"]["orientation"] == "black"

    def test_finished_game_falls_back_to_stored(self, mock_client, auth_client, user):
        # No longer current on Chess.com...
        mock_client.return_value.game_detail.return_value = None
        # ...but a snapshot exists — served as a finished (review) game.
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            pgn=PGN,
            fen=FEN_START,
            is_active=False,
        )

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert response.context["is_live"] is False
        data = _review_data(response)
        assert data["meta"]["isLive"] is False
        assert [p["san"] for p in data["plies"]] == ["e4", "e5", "Nf3", "Nc6"]

    def test_embeds_completed_analysis(self, mock_client, auth_client, user):
        from chessdotcom_ai_coach.services import board as board_utils

        mock_client.return_value.game_detail.return_value = _sample_game()
        # Coach analysed the move-2 (White) position and recommended the played move.
        move_fen = board_utils.moves_from_pgn(PGN)[2]["fen_before"]
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=move_fen,
            status=CoachSuggestion.Status.DONE,
            eval_text="+0.3",
            eval_cp=0.3,
            best_move_san="Nf3",
            best_move_uci="g1f3",
            analysis="Develop the knight.",
        )

        response = auth_client.get("/game/944768131")

        data = _review_data(response)
        # Analysis is keyed by 1-based ply index; ply 3 is White's 2nd move.
        assert data["analysis"]["3"]["recSan"] == "Nf3"
        assert data["analysis"]["3"]["followed"] is True
        assert data["analysis"]["3"]["recFrom"] == "g1"

    def test_not_found(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = None

        response = auth_client.get("/game/nope")

        assert response.status_code == 404
        assert b"Game not found" in response.content

    def test_error_page_on_failure(self, mock_client, auth_client):
        mock_client.return_value.game_detail.side_effect = Exception("boom")

        response = auth_client.get("/game/944768131")

        assert response.status_code == 500

    def test_poll_returns_json(self, mock_client, auth_client):
        mock_client.return_value.game_detail.return_value = _sample_game()

        response = auth_client.get("/game/944768131", {"poll": "1"})

        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json")
        body = response.json()
        assert body["meta"]["isLive"] is True
        assert body["meta"]["liveHead"] == 4

    def test_poll_transient_failure_skips(self, mock_client, auth_client):
        mock_client.return_value.game_detail.side_effect = Exception("boom")

        response = auth_client.get("/game/944768131", {"poll": "1"})

        # Poll swallows a transient fetch failure so the client keeps polling.
        assert response.status_code == 204


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.analyze_game_task")
@patch("chessdotcom_ai_coach.views.Client")
class TestAnalyzePosition:
    def test_post_enqueues_once_and_creates_pending(
        self, mock_client, mock_task, auth_client, user
    ):
        mock_client.return_value.game_detail.return_value = _sample_game()

        response = auth_client.post(
            "/game/944768131/analyze", {"fen": FEN_START}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        mock_task.delay.assert_called_once()
        row = CoachSuggestion.objects.get(user=user, game_id="944768131", fen=FEN_START)
        assert row.status == CoachSuggestion.Status.PENDING

    def test_finished_game_does_not_call_chess_com(
        self, mock_client, mock_task, auth_client, user
    ):
        # A finished snapshot is the source of truth: analysis must not hit
        # Chess.com just to fetch the PGN.
        Game.objects.create(
            user=user,
            game_id="944768131",
            white_name="MyUser",
            black_name="Opponent",
            pgn=PGN,
            fen=FEN_START,
            is_active=False,
        )

        response = auth_client.post("/game/944768131/analyze", {"fen": FEN_START})

        assert response.status_code == 200
        mock_client.return_value.game_detail.assert_not_called()
        # The task is still enqueued with the snapshot's PGN.
        mock_task.delay.assert_called_once()
        assert mock_task.delay.call_args.args[3] == PGN

    def test_post_is_idempotent_while_pending(
        self, mock_client, mock_task, auth_client, user
    ):
        mock_client.return_value.game_detail.return_value = _sample_game()

        auth_client.post("/game/944768131/analyze", {"fen": FEN_START})
        auth_client.post("/game/944768131/analyze", {"fen": FEN_START})

        mock_task.delay.assert_called_once()
        assert CoachSuggestion.objects.filter(fen=FEN_START).count() == 1

    def test_get_reports_done_status(
        self, mock_client, mock_task, auth_client, user
    ):
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=FEN_START,
            status=CoachSuggestion.Status.DONE,
            eval_text="+0.2",
            eval_cp=0.2,
            best_move_san="e4",
            best_move_uci="e2e4",
            analysis="Play e4.",
        )

        response = auth_client.get("/game/944768131/analyze", {"fen": FEN_START})

        body = response.json()
        assert body["status"] == "done"
        assert body["recSan"] == "e4"
        assert body["recFrom"] == "e2"
        assert body["prose"] == "Play e4."
        mock_task.delay.assert_not_called()

    def test_missing_fen_is_bad_request(
        self, mock_client, mock_task, auth_client, user
    ):
        response = auth_client.post("/game/944768131/analyze", {})

        assert response.status_code == 400


@pytest.mark.django_db
class TestLogout:
    def test_redirects_to_login(self, auth_client):
        response = auth_client.get("/logout")

        assert response.status_code == 302
        assert response["Location"] == "/login"
