"""Unit tests for the Game Review page and its on-demand analysis endpoint.

The review is a client-side, move-by-move walk over a *finished* game: the view
serialises the stored PGN + coach analysis into JSON the page steps through, and
``review_analyze`` lets the user request the coach for a single past position
(reusing the same Celery task as the live coach panel, mocked here).
"""

import json
from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game

PGN = '[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *'


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client


def _stored_game(user, **overrides):
    defaults = dict(
        user=user,
        game_id="944768131",
        white_name="MyUser",
        black_name="Opponent",
        white_rating="1842",
        black_rating="1867",
        time_class="rapid",
        pgn=PGN,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        is_active=False,
    )
    defaults.update(overrides)
    return Game.objects.create(**defaults)


def _review_data(response):
    """Pull the JSON model the view embedded via ``json_script`` for the client."""
    marker = b'<script id="gr-data" type="application/json">'
    body = response.content
    start = body.index(marker) + len(marker)
    end = body.index(b"</script>", start)
    return json.loads(body[start:end].decode())


@pytest.mark.django_db
class TestGameReview:
    def test_renders_and_serialises_plies(self, auth_client, user):
        _stored_game(user)

        response = auth_client.get("/game/944768131/review")

        assert response.status_code == 200
        assert b'id="gr-board"' in response.content
        data = _review_data(response)
        assert [p["san"] for p in data["plies"]] == ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]
        # positions are index-aligned: one more than the plies.
        assert len(data["positions"]) == len(data["plies"]) + 1
        # from/to squares are derived for board highlighting + arrows.
        assert data["plies"][0]["from"] == "e2"
        assert data["plies"][0]["to"] == "e4"

    def test_orientation_black_when_user_is_black(self, auth_client, user):
        _stored_game(user, white_name="Opponent", black_name="MyUser")

        response = auth_client.get("/game/944768131/review")

        assert response.status_code == 200
        assert _review_data(response)["meta"]["orientation"] == "black"

    def test_embeds_completed_analysis(self, auth_client, user):
        game = _stored_game(user)
        moves_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        # The move-2 (white) position: coach recommended the played move, Nf3.
        from chessdotcom_ai_coach.services import board as board_utils

        move_fen = board_utils.moves_from_pgn(PGN)[2]["fen_before"]
        CoachSuggestion.objects.create(
            user=user,
            game_id=game.game_id,
            fen=move_fen,
            status=CoachSuggestion.Status.DONE,
            eval_text="+0.3",
            eval_cp=0.3,
            best_move_san="Nf3",
            best_move_uci="g1f3",
            analysis="Develop the knight.",
        )

        response = auth_client.get("/game/944768131/review")

        data = _review_data(response)
        # Analysis is keyed by 1-based ply index; ply 3 is White's 2nd move.
        assert data["analysis"]["3"]["recSan"] == "Nf3"
        assert data["analysis"]["3"]["followed"] is True
        assert data["analysis"]["3"]["recFrom"] == "g1"

    def test_404_when_game_not_stored(self, auth_client):
        response = auth_client.get("/game/nope/review")

        assert response.status_code == 404
        assert b"Game not found" in response.content

    def test_message_when_no_pgn(self, auth_client, user):
        _stored_game(user, pgn="")

        response = auth_client.get("/game/944768131/review")

        assert response.status_code == 200
        assert b"be reviewed yet" in response.content

    def test_requires_login(self, client):
        response = client.get("/game/944768131/review")

        assert response.status_code == 302
        assert "/login" in response["Location"]

    def test_past_games_link_to_review(self, auth_client, user):
        _stored_game(user)

        response = auth_client.get("/games")

        assert b"/game/944768131/review" in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.analyze_game_task")
class TestReviewAnalyze:
    FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def test_post_enqueues_once_and_creates_pending(self, mock_task, auth_client, user):
        _stored_game(user)

        response = auth_client.post(
            "/game/944768131/review/analyze", {"fen": self.FEN}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        mock_task.delay.assert_called_once()
        row = CoachSuggestion.objects.get(user=user, game_id="944768131", fen=self.FEN)
        assert row.status == CoachSuggestion.Status.PENDING

    def test_post_is_idempotent_while_pending(self, mock_task, auth_client, user):
        _stored_game(user)

        auth_client.post("/game/944768131/review/analyze", {"fen": self.FEN})
        auth_client.post("/game/944768131/review/analyze", {"fen": self.FEN})

        # A still-pending position is not re-enqueued.
        mock_task.delay.assert_called_once()
        assert CoachSuggestion.objects.filter(fen=self.FEN).count() == 1

    def test_get_reports_done_status(self, mock_task, auth_client, user):
        _stored_game(user)
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=self.FEN,
            status=CoachSuggestion.Status.DONE,
            eval_text="+0.2",
            eval_cp=0.2,
            best_move_san="e4",
            best_move_uci="e2e4",
            analysis="Play e4.",
        )

        response = auth_client.get(
            "/game/944768131/review/analyze", {"fen": self.FEN}
        )

        body = response.json()
        assert body["status"] == "done"
        assert body["recSan"] == "e4"
        assert body["recFrom"] == "e2"
        assert body["prose"] == "Play e4."
        # A pure GET status poll never enqueues work.
        mock_task.delay.assert_not_called()

    def test_missing_fen_is_a_bad_request(self, mock_task, auth_client, user):
        _stored_game(user)

        response = auth_client.post("/game/944768131/review/analyze", {})

        assert response.status_code == 400
