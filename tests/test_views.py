"""Unit tests for the views.

The detail page reads entirely from the stored ``Game`` snapshot (kept fresh by
the scheduler) and ``CoachSuggestion`` rows — no Chess.com call — so these tests
just seed the DB. The Celery task is mocked where analysis is enqueued.
"""

from unittest.mock import patch

import pytest
from django.conf import settings

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


def _ply_fen(idx):
    """The ``fen_before`` for a 0-based ply of ``PGN`` (the coach's join key)."""
    return board_utils.moves_from_pgn(PGN)[idx]["fen_before"]


def _make_suggestion(user, fen, **overrides):
    """Seed a ``CoachSuggestion`` row (DONE by default) for the standard game."""
    defaults = dict(
        user=user,
        game_id="944768131",
        fen=fen,
        status=CoachSuggestion.Status.DONE,
        eval_text="+0.3",
        eval_cp=0.3,
        best_move_san="Nf3",
        best_move_uci="g1f3",
        analysis="Develop the knight.",
    )
    defaults.update(overrides)
    return CoachSuggestion.objects.create(**defaults)


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
        # The card follows the PGN (4 plies in), not the stale seeded FEN.
        assert games[0].move_no == 3

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
class TestGameCardPosition:
    """The home card's mini board follows the PGN, like the detail page does.

    Chess.com's ``fen`` field can lag its own movetext and is empty on some
    payloads, which used to render the card as an untouched starting position.
    """

    def test_card_follows_the_pgn_not_a_stale_fen(self, auth_client, user):
        _make_game(user, fen=FEN_START)  # stale: says nothing has been played

        response = auth_client.get("/games")
        game = response.context["games"][0]

        expected = board_utils.positions_from_pgn(PGN)[-1]
        assert game.cells == board_utils.fen_to_cells(expected)
        assert game.move_no == 3
        assert game.turn == "white"

    def test_card_falls_back_to_the_stored_fen_without_a_pgn(self, auth_client, user):
        _make_game(user, pgn="", fen=FEN_LIVE)

        game = auth_client.get("/games").context["games"][0]

        assert game.cells == board_utils.fen_to_cells(FEN_LIVE)
        assert game.move_no == 3
        assert game.turn == "white"

    def test_card_without_pgn_or_fen_is_the_untouched_board(self, auth_client, user):
        _make_game(user, pgn="", fen="")

        game = auth_client.get("/games").context["games"][0]

        # Documented fallback: nothing to draw → the starting position, and the
        # template hides the move number behind `{% if game.move_no %}`.
        assert game.cells == board_utils.fen_to_cells(None)
        assert game.move_no is None

    def test_past_card_shows_the_pgn_position(self, auth_client, user):
        _make_game(user, is_active=False, fen=FEN_START)

        game = auth_client.get("/games").context["past_games"][0]

        assert game.cells == board_utils.fen_to_cells(
            board_utils.positions_from_pgn(PGN)[-1]
        )
        assert game.move_no == 3

    def test_your_turn_badge_follows_the_pgn(self, auth_client, user):
        # The user is White. The stale FEN claims White is to move, but the PGN
        # says White has already played 1. e4 — so it is not their turn.
        _make_game(user, pgn='[Event "Test"]\n\n1. e4 *', fen=FEN_START)

        response = auth_client.get("/games")

        assert response.context["games"][0].is_user_turn is False
        assert "Your turn" not in response.content.decode()


@pytest.mark.django_db
class TestGameDetail:
    """The unified detail page: same UI for live and finished games."""

    def test_live_game_renders_at_head(self, auth_client, user):
        _make_game(user)  # active, user is White and to move

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content
        assert b"WATCHING LIVE" in response.content
        # Your turn: the page opens on the live slot, one past the last played ply.
        assert response.context["head"] == 4
        assert response.context["sel"] == response.context["live_sel"] == 5
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

    def test_full_render_has_no_out_of_band_swaps(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        # The arrow overlay, eval bar, analysis history and moves grid are always
        # present as stable OOB targets, but in a full #gr-view render they are
        # inline — not out-of-band (that is only for the standalone coach-card
        # self-poll).
        assert 'id="gr-arrows"' in body
        assert 'id="gr-evalfill"' in body
        assert 'id="gr-history"' in body
        assert 'id="gr-moves-panel"' in body
        assert 'id="gr-arrows" hx-swap-oob' not in body
        assert 'id="gr-evalfill" hx-swap-oob' not in body
        assert 'id="gr-history" hx-swap-oob' not in body
        assert 'id="gr-moves-panel" hx-swap-oob' not in body

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

    def test_get_syncs_board_out_of_band(self, mock_task, auth_client, user):
        _make_game(user)  # live, White (user) to move — the live slot is sel 5
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=FEN_LIVE,
            status=CoachSuggestion.Status.DONE,
            eval_text="+0.4",
            eval_cp=0.4,
            best_move_san="Bb5",
            best_move_uci="f1b5",
            analysis="Pin the knight.",
        )

        response = auth_client.get("/game/944768131/analyze", {"sel": "5"})
        body = response.content.decode()

        # The card body updated…
        assert "BEST MOVE" in body
        # …and the board arrows + eval bar ride along out-of-band so the board
        # updates without a full #gr-view re-render.
        assert 'id="gr-arrows"' in body
        assert 'id="gr-evalfill"' in body
        assert 'hx-swap-oob="true"' in body
        assert 'stroke="#b78e54"' in body  # brass recommended-move arrow

    def test_get_adds_the_history_slot_out_of_band(self, mock_task, auth_client, user):
        _make_game(user, is_active=False)
        # sel 3 is White's 2nd move (Nf3) — the coach recommended it too.
        _make_suggestion(user, _ply_fen(2))

        response = auth_client.get("/game/944768131/analyze", {"sel": "3"})
        body = response.content.decode()

        # The freshly-arrived analysis gets its slot in the timeline and badges
        # the move in the grid, both out-of-band — no full #gr-view re-render.
        assert 'id="gr-history" hx-swap-oob="true"' in body
        assert "Analysis history" in body
        assert "Develop the knight." in body
        assert 'class="gr-card__count">1<' in body
        assert 'id="gr-moves-panel" hx-swap-oob="true"' in body
        assert "gr-badge--followed" in body

    def test_post_badges_the_move_as_pending_out_of_band(
        self, mock_task, auth_client, user
    ):
        _make_game(user, is_active=False)

        response = auth_client.post("/game/944768131/analyze", {"sel": "3"})
        body = response.content.decode()

        # Enqueueing marks the move pending in the grid straight away; nothing
        # is done yet, so the history stays empty.
        assert 'id="gr-moves-panel" hx-swap-oob="true"' in body
        assert "gr-badge--pending" in body
        assert 'id="gr-history" hx-swap-oob="true"' in body
        assert 'class="gr-card__count">0<' in body

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


@pytest.mark.django_db
class TestLoginPage:
    """The login page markup and the auth round-trip (Django's LoginView)."""

    def test_renders_form_without_app_header(self, client):
        response = client.get("/login")
        body = response.content.decode()

        assert response.status_code == 200
        assert 'id="id_username"' in body
        assert 'id="id_password"' in body
        assert 'name="username"' in body and 'name="password"' in body
        # Login overrides {% block header %} to nothing — no app chrome.
        assert 'class="app-header"' not in body

    def test_invalid_credentials_show_error(self, client, user):
        response = client.post(
            "/login", {"username": "MyUser", "password": "wrong"}
        )

        assert response.status_code == 200
        assert b"Invalid username or password." in response.content

    def test_valid_credentials_redirect_home(self, client, user):
        response = client.post(
            "/login", {"username": "MyUser", "password": "pw12345!"}
        )

        assert response.status_code == 302
        assert response["Location"] == "/"


@pytest.mark.django_db
class TestCoachCardModes:
    """Every branch of partials/coach_card.html, driven by ``coach.mode``."""

    def test_opponent_move(self, auth_client, user):
        _make_game(user, is_active=False)

        # sel 2 is Black's move (e5) — the opponent's.
        response = auth_client.get("/game/944768131/view", {"sel": "2"})

        assert response.context["coach"]["mode"] == "opponent"
        assert b"The coach only analyses your moves" in response.content

    def test_unanalyzed_shows_request_button(self, auth_client, user):
        _make_game(user, is_active=False)

        # sel 3 is White's Nf3 (a user move) with no suggestion yet.
        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "unanalyzed"
        assert "Request suggestion" in body
        assert 'hx-post="/game/944768131/analyze?sel=3"' in body
        assert 'hx-target="#gr-coach"' in body

    def test_pending_self_polls(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "pending"
        assert "spinner" in body
        assert 'hx-trigger="every 2s"' in body
        assert 'hx-get="/game/944768131/analyze?sel=3"' in body

    def test_analyzed_followed(self, auth_client, user):
        _make_game(user, is_active=False)
        # Best move equals the move actually played (Nf3) → "followed".
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3")

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "analyzed"
        assert response.context["coach"]["followed"] is True
        assert "You played the best move" in body
        assert "gr-status--followed" in body
        assert "gr-compare__cell--good" in body
        # No "Played" legend entry / played-move arrow when followed.
        assert "gr-legend__dot--green" not in body

    def test_analyzed_differed(self, auth_client, user):
        _make_game(user, is_active=False)
        # Best move differs from the played Nf3 → "differed".
        _make_suggestion(
            user, _ply_fen(2), best_move_san="d4", best_move_uci="d2d4"
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "analyzed"
        assert response.context["coach"]["followed"] is False
        assert "The coach preferred" in body
        assert "gr-status--differed" in body
        assert "gr-compare__cell--diff" in body
        # A "Played" legend entry appears alongside the brass "Recommended" one.
        assert "gr-legend__dot--green" in body
        assert 'stroke="#4a7a52"' in body  # green played-move arrow

    def test_live_waiting_for_opponent(self, auth_client, user):
        # Live game, Black to move while the user is White → waiting.
        _make_game(
            user,
            fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 2 3",
        )

        response = auth_client.get("/game/944768131")

        assert response.context["coach"]["mode"] == "live_waiting"
        assert b"Opponent to move" in response.content

    def test_live_request_for_user_move(self, auth_client, user):
        _make_game(user)  # live, White (user) to move, no suggestion yet

        response = auth_client.get("/game/944768131")

        assert response.context["coach"]["mode"] == "live_request"
        assert b"Ask the coach what to play" in response.content

    def test_live_pending_self_polls(self, auth_client, user):
        _make_game(user)  # live head at FEN_LIVE, user to move
        _make_suggestion(
            user, FEN_LIVE, status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131")
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "live_pending"
        assert "Analysing the current position" in body
        assert 'hx-trigger="every 2s"' in body

    def test_live_head_reviews_your_just_played_move(self, auth_client, user):
        # Live game whose last ply is the user's OWN move (White Nf3), with the
        # opponent now on the clock. The just-played move must still show its
        # coach review and board arrows — not the bare "waiting" state.
        live_pgn = '[Event "Test"]\n\n1. e4 e5 2. Nf3 *'
        _make_game(
            user,
            pgn=live_pgn,
            fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
        )
        # Suggestion for that move (position before White's 2. Nf3); best move
        # differs from the played Nf3 → both recommended and played arrows.
        fen_before = board_utils.moves_from_pgn(live_pgn)[2]["fen_before"]
        _make_suggestion(
            user, fen_before, best_move_san="d4", best_move_uci="d2d4"
        )

        response = auth_client.get("/game/944768131")
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "analyzed"
        assert 'stroke="#b78e54"' in body  # brass recommended arrow
        assert 'stroke="#4a7a52"' in body  # green played-move arrow


@pytest.mark.django_db
class TestMovesGrid:
    """partials/moves_grid.html — the click-to-review move list."""

    def test_each_move_links_to_its_position(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert 'hx-get="/game/944768131/view?sel=1"' in body
        assert 'hx-target="#gr-view"' in body
        # The selected ply is flagged.
        assert "gr-move--sel" in body

    def test_analyzed_move_shows_followed_badge(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3")

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert "gr-move--analyzed" in body
        assert "gr-badge--followed" in body

    def test_pending_move_shows_pending_badge(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert b"gr-badge--pending" in response.content


@pytest.mark.django_db
class TestLiveMoveSlot:
    """The provisional grid item for the move you're about to play.

    On a live game where it's the user's turn the slot owns its own cursor,
    ``live_sel`` (``head + 1`` — here 5), so the opponent's last move stays
    reviewable at ``head``.
    """

    def test_live_turn_shows_an_empty_slot(self, auth_client, user):
        _make_game(user)  # live, White (user) to move, no suggestion yet

        response = auth_client.get("/game/944768131/view", {"sel": "5"})
        body = response.content.decode()

        assert "gr-move--live" in body
        assert "your move" in body
        # Nothing requested yet — no badge on the slot.
        assert "gr-badge--live" not in body
        assert "gr-badge--pending" not in body

    def test_pending_suggestion_badges_the_slot(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, FEN_LIVE, status=CoachSuggestion.Status.PENDING)

        response = auth_client.get("/game/944768131/view", {"sel": "5"})
        body = response.content.decode()

        assert "gr-move--live" in body
        assert "gr-badge--pending" in body

    def test_done_suggestion_shows_the_recommendation_before_you_play(
        self, auth_client, user
    ):
        _make_game(user)
        _make_suggestion(user, FEN_LIVE, best_move_san="Bb5")

        response = auth_client.get("/game/944768131/view", {"sel": "5"})
        body = response.content.decode()

        # The move isn't played yet — the grid still holds only the 4 PGN plies,
        # but the coach's pick is already visible on the live slot.
        assert "gr-badge--live" in body
        assert "Bb5" in body

    def test_slot_owns_the_selection_at_the_live_head(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "5"})
        body = response.content.decode()

        assert body.count("gr-move--sel") == 1
        assert "gr-move--live gr-move--sel" in body

    def test_opponents_last_move_stays_reviewable(self, auth_client, user):
        _make_game(user)  # live, your turn — head 4 is Black's Nc6

        response = auth_client.get("/game/944768131/view", {"sel": "4"})
        body = response.content.decode()

        # Stepping back from the live slot reviews the opponent's move rather
        # than falling through to the live view.
        assert response.context["coach"]["mode"] == "opponent"
        assert "Reviewing: 2… Nc6" in body
        # …and the selection is on that ply, not on the live slot.
        assert body.count("gr-move--sel") == 1
        assert "gr-move--live gr-move--sel" not in body

    def test_back_from_the_live_slot_lands_on_the_last_ply(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "5"})

        assert response.context["prev_sel"] == 4
        assert response.context["next_sel"] == 5

    def test_no_slot_while_the_opponent_is_on_the_clock(self, auth_client, user):
        # Black to move: the coach has nothing to suggest for the user yet.
        _make_game(user, fen=FEN_LIVE.replace(" w ", " b "))

        response = auth_client.get("/game/944768131/view", {"sel": "5"})

        assert b"gr-move--live" not in response.content
        # The timeline ends at the last played ply.
        assert response.context["sel"] == response.context["live_sel"] == 4

    def test_no_slot_on_a_finished_game(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "5"})

        assert b"gr-move--live" not in response.content
        assert response.context["sel"] == 4

    def test_arriving_suggestion_updates_the_slot_out_of_band(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, FEN_LIVE, best_move_san="Bb5")

        # The `live_pending` self-poll lands on the analyze endpoint.
        response = auth_client.get("/game/944768131/analyze", {"sel": "5"})
        body = response.content.decode()

        assert 'id="gr-moves-panel" hx-swap-oob="true"' in body
        assert "gr-move--live" in body
        assert "gr-badge--live" in body
        assert "Bb5" in body


@pytest.mark.django_db
class TestHistoryList:
    """partials/history_list.html — the analysed-moves timeline."""

    def test_counts_and_lists_analysed_user_moves(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3")  # followed

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["history_count"] == 1
        assert 'class="gr-card__count">1<' in body
        assert 'hx-get="/game/944768131/view?sel=3"' in body
        assert "gr-tag--followed" in body

    def test_empty_when_no_analysis(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "1"})

        assert response.context["history_count"] == 0
        assert b"gr-tag--" not in response.content


@pytest.mark.django_db
class TestBoardRendering:
    """partials/board.html expanded from the stored FEN."""

    def test_renders_64_cells(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "0"})

        assert len(response.context["cells"]) == 64

    def test_last_move_highlights_two_squares(self, auth_client, user):
        _make_game(user, is_active=False)

        # sel 3 is Nf3 (g1→f3): both squares ringed.
        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        highlighted = [c for c in response.context["cells"] if c["highlight"]]

        assert len(highlighted) == 2
        assert b"board__sq--hl" in response.content

    def test_black_orientation_reverses_cells(self, auth_client, user):
        # Same starting position, once as White and once as Black.
        white_game = _make_game(user, game_id="white1", is_active=False, fen=FEN_START)
        black_game = _make_game(
            user,
            game_id="black1",
            white_name="Opponent",
            black_name="MyUser",
            is_active=False,
            fen=FEN_START,
        )

        white_cells = auth_client.get(
            f"/game/{white_game.game_id}/view", {"sel": "0"}
        ).context["cells"]
        black_cells = auth_client.get(
            f"/game/{black_game.game_id}/view", {"sel": "0"}
        ).context["cells"]

        assert black_cells == list(reversed(white_cells))


@pytest.mark.django_db
class TestNavigation:
    """The move navigation bar in partials/position.html."""

    def test_prev_next_head_and_button_targets(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()
        ctx = response.context

        assert (ctx["prev_sel"], ctx["next_sel"], ctx["head"]) == (2, 4, 4)
        assert 'hx-get="/game/944768131/view?sel=0"' in body  # start
        assert 'hx-get="/game/944768131/view?sel=2"' in body  # back
        assert 'hx-get="/game/944768131/view?sel=4"' in body  # forward / end

    def test_live_game_shows_jump_to_live_button(self, auth_client, user):
        _make_game(user)  # live, head == 4

        # Review an earlier ply on the live game (htmx fragment).
        response = auth_client.get(
            "/game/944768131/view", {"sel": "2"}, HTTP_HX_REQUEST="true"
        )
        body = response.content.decode()

        assert response.context["behind"] == 2
        assert "gr-live-btn" in body
        assert "2 new · live" in body
        assert 'hx-get="/game/944768131/view?sel=4"' in body  # jump to head


@pytest.mark.django_db
class TestHtmxFragments:
    """HTMX-specific rendering: the out-of-band header pill sync."""

    def test_htmx_request_syncs_pill_out_of_band(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get(
            "/game/944768131/view", {"sel": "3"}, HTTP_HX_REQUEST="true"
        )
        body = response.content.decode()

        assert 'id="gr-pill" hx-swap-oob="true"' in body

    def test_non_htmx_request_has_no_pill_swap(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert 'id="gr-pill" hx-swap-oob="true"' not in body


@pytest.mark.django_db
class TestGameListStates:
    """partials/game_list.html — empty state and the your-turn card."""

    def test_empty_state(self, auth_client):
        response = auth_client.get("/games")

        assert b"No active games" in response.content

    def test_your_turn_card(self, auth_client, user):
        # The turn is read off the PGN: it ends on a Black ply, so White (the
        # user) is to move → your turn.
        _make_game(user, fen=FEN_START)

        response = auth_client.get("/games")
        body = response.content.decode()

        assert "gm-card--your-turn" in body
        assert "Your turn" in body
        assert "LIVE" in body


@pytest.mark.django_db
class TestEvalBar:
    """The eval bar fill (_eval_fill / partials/_evalfill.html)."""

    def test_defaults_to_midpoint_without_analysis(self, auth_client, user):
        _make_game(user, is_active=False)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["eval_fill"] == 50
        assert b"height:50%" in response.content

    def test_clamps_a_large_positive_eval(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3", eval_cp=10)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["eval_fill"] == 93
        assert b"height:93%" in response.content

    def test_clamps_a_large_negative_eval(self, auth_client, user):
        _make_game(user, is_active=False)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3", eval_cp=-10)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["eval_fill"] == 7
        assert b"height:7%" in response.content


@pytest.mark.django_db
class TestVersionBadge:
    def test_header_shows_version(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/")

        assert b'class="brand__version"' in response.content
        assert f"v{settings.APP_VERSION}".encode() in response.content

    def test_login_page_has_no_version(self, client):
        response = client.get("/login")

        assert b"brand__version" not in response.content
