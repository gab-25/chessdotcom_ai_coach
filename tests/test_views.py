"""Unit tests for the views.

The detail page reads entirely from the stored ``Game`` snapshot (kept fresh by
the scheduler) and ``CoachSuggestion`` rows — no Chess.com call — so these tests
just seed the DB. The Celery task is mocked where analysis is enqueued.
"""

from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services import board as board_utils

FEN_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Position after 1. e4 e5 2. Nf3 Nc6 — White (the user) to move.
FEN_LIVE = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
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


def _make_game(user, **overrides):
    defaults = dict(
        user=user,
        game_id="944768131",
        white_name="MyUser",
        black_name="Opponent",
        white_rating="1500",
        black_rating="1600",
        time_class="rapid",
        pgn=PGN,
        fen=FEN_LIVE,
        is_active=True,
    )
    defaults.update(overrides)
    return Game.objects.create(**defaults)


@pytest.mark.django_db
class TestHome:
    def test_lists_games(self, auth_client, user):
        _make_game(user, fen=FEN_START)

        response = auth_client.get("/")

        assert response.status_code == 200
        games = list(response.context["games"])
        assert len(games) == 1
        assert games[0].game_id == "944768131"
        assert len(games[0].cells) == 64
        assert games[0].move_no == 1

    def test_does_not_call_chess_com(self, auth_client):
        with patch("chessdotcom_ai_coach.services.chess_client.Client") as mock_client:
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
        _make_game(user)

        response = auth_client.get("/games")

        assert response.status_code == 200
        assert b"Opponent" in response.content

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
class TestGameDetail:
    """The unified detail page: same UI for live and finished games."""

    def test_live_game_renders_at_head(self, auth_client, user):
        _make_game(user)  # active, user is White and to move

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content
        assert b"WATCHING LIVE" in response.content
        assert response.context["sel"] == response.context["head"] == 4
        # At the live head, your move, no suggestion yet → request button.
        assert b"Request suggestion" in response.content

    def test_finished_game_starts_at_opening(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert b"REVIEW" in response.content
        assert response.context["sel"] == 0
        assert b"Step through the moves" in response.content

    def test_orientation_black_when_user_is_black(self, auth_client, user):
        _make_game(user, white_name="Opponent", black_name="MyUser", is_active=False)

        response = auth_client.get("/game/944768131")

        assert response.context["orientation"] == "black"

    def test_not_found(self, auth_client):
        response = auth_client.get("/game/nope")

        assert response.status_code == 404
        assert b"Game not found" in response.content

    def test_requires_login(self, client):
        response = client.get("/game/944768131")

        assert response.status_code == 302

    def test_position_fragment_for_a_ply(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content
        assert b"Reviewing" in response.content

    def test_embeds_completed_analysis(self, auth_client, user):
        _make_game(user, is_active=False)
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

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert b"BEST MOVE" in response.content
        assert b"You played the best move" in response.content

    def test_live_poll_204_when_no_new_move(self, auth_client, user):
        _make_game(user)  # head == 4

        response = auth_client.get(
            "/game/944768131/live", {"sel": "4", "head": "4"}
        )

        assert response.status_code == 204

    def test_live_poll_swaps_when_new_move(self, auth_client, user):
        _make_game(user)  # head == 4 now; client still thinks head == 3

        response = auth_client.get(
            "/game/944768131/live", {"sel": "3", "head": "3"}
        )

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.analyze_game_task")
class TestAnalyzePosition:
    def test_post_enqueues_for_a_user_move(self, mock_task, auth_client, user):
        _make_game(user, is_active=False)

        # sel 3 is White's 2nd move (Nf3) — a user move.
        response = auth_client.post("/game/944768131/analyze", {"sel": "3"})

        assert response.status_code == 200
        assert b"Analysing" in response.content
        mock_task.delay.assert_called_once()
        move_fen = board_utils.moves_from_pgn(PGN)[2]["fen_before"]
        row = CoachSuggestion.objects.get(user=user, game_id="944768131", fen=move_fen)
        assert row.status == CoachSuggestion.Status.PENDING

    def test_post_is_noop_for_opponent_move(self, mock_task, auth_client, user):
        _make_game(user, is_active=False)

        # sel 2 is Black's move (e5) — the coach only analyses the user's moves.
        response = auth_client.post("/game/944768131/analyze", {"sel": "2"})

        assert response.status_code == 200
        mock_task.delay.assert_not_called()
        assert CoachSuggestion.objects.count() == 0

    def test_get_returns_the_done_card(self, mock_task, auth_client, user):
        _make_game(user, is_active=False)
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

        response = auth_client.get("/game/944768131/analyze", {"sel": "3"})

        assert b"BEST MOVE" in response.content
        assert b"Develop the knight." in response.content
        mock_task.delay.assert_not_called()

    def test_analysis_never_calls_chess_com(self, mock_task, auth_client, user):
        _make_game(user, is_active=False)

        with patch("chessdotcom_ai_coach.services.chess_client.Client") as mock_client:
            auth_client.post("/game/944768131/analyze", {"sel": "3"})

        mock_client.assert_not_called()


@pytest.mark.django_db
class TestLogout:
    def test_redirects_to_login(self, auth_client):
        response = auth_client.get("/logout")

        assert response.status_code == 302
        assert response["Location"] == "/login"
