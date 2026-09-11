"""Unit tests for the views.

The app shows finished games only, so ``_make_game`` builds one by default
(``is_active=False``); the few tests that need a game still in progress pass
``is_active=True`` and assert it is refused. The detail page reads entirely from
the stored ``Game`` snapshot and ``CoachSuggestion`` rows — no Chess.com call —
so these tests just seed the DB. The Celery task is mocked where analysis is
enqueued.
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
        is_active=False,  # finished: the only kind the app shows
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

    def test_has_refresh_button(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/")

        assert b'id="game-list"' in response.content
        assert b'hx-target="#game-list"' in response.content
        assert b'hx-get="/games"' in response.content

    def test_has_no_auto_refresh(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/")

        assert b"every 5s" not in response.content
        assert b"AUTO-REFRESH" not in response.content

    def test_shows_game_count_once(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/")

        assert response.content.count(b'id="home-count"') == 1
        assert b"1 finished game to review" in response.content
        assert b"hx-swap-oob" not in response.content

    def test_does_not_list_a_game_in_progress(self, auth_client, user):
        """A game still being played has nothing to review, so it isn't shown."""
        _make_game(user, is_active=True)

        response = auth_client.get("/")

        assert list(response.context["games"]) == []
        assert b"944768131" not in response.content
        assert b"No games to review" in response.content


@pytest.mark.django_db
class TestGameList:
    def test_returns_fragment_with_games(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/games")

        assert response.status_code == 200
        assert b"Opponent" in response.content

    def test_renders_finished_games(self, auth_client, user):
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b"PastFoe" in response.content
        assert b"REVIEW" in response.content

    def test_omits_a_game_in_progress(self, auth_client, user):
        _make_game(user, is_active=True)
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b"PastFoe" in response.content
        assert b"944768131" not in response.content

    def test_finished_games_link_to_detail(self, auth_client, user):
        Game.objects.create(
            user=user, game_id="old1", black_name="PastFoe", is_active=False
        )

        response = auth_client.get("/games")

        assert b'href="/game/old1"' in response.content

    def test_updates_game_count_out_of_band(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/games")

        assert b'id="home-count"' in response.content
        assert b'hx-swap-oob="true"' in response.content
        assert b"1 finished game to review" in response.content


@pytest.mark.django_db
class TestGameDetail:
    """The detail page: review of a finished game, and only a finished game."""

    def test_finished_game_starts_at_opening(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131")

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content
        assert response.context["sel"] == 0
        assert response.context["head"] == 4  # the timeline ends on the last ply
        assert b"Step through the moves" in response.content

    def test_game_in_progress_is_refused(self, auth_client, user):
        """Nothing about a running game is shown, hand-typed URL included."""
        _make_game(user, is_active=True)

        response = auth_client.get("/game/944768131")

        assert response.status_code == 404
        assert b"still in progress" in response.content
        assert b'id="gr-view"' not in response.content

    def test_position_fragment_refuses_a_game_in_progress(self, auth_client, user):
        _make_game(user, is_active=True)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.status_code == 404

    def test_never_polls_for_live_updates(self, auth_client, user):
        """There is no live poll left: the page is a static review."""
        _make_game(user)

        response = auth_client.get("/game/944768131")

        assert b"every 5s" not in response.content
        assert b"/live" not in response.content

    def test_orientation_black_when_user_is_black(self, auth_client, user):
        _make_game(user, white_name="Opponent", black_name="MyUser")

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
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.status_code == 200
        assert b'id="gr-view"' in response.content
        assert b"Reviewing" in response.content

    def test_full_render_has_no_out_of_band_swaps(self, auth_client, user):
        _make_game(user)

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
        _make_game(user)
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


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.views.analyze_game_task")
class TestAnalyzePosition:
    def test_post_enqueues_for_a_user_move(self, mock_task, auth_client, user):
        _make_game(user)

        # sel 3 is White's 2nd move (Nf3) — a user move.
        response = auth_client.post("/game/944768131/analyze", {"sel": "3"})

        assert response.status_code == 200
        assert b"Analysing" in response.content
        mock_task.delay.assert_called_once()
        move_fen = board_utils.moves_from_pgn(PGN)[2]["fen_before"]
        row = CoachSuggestion.objects.get(user=user, game_id="944768131", fen=move_fen)
        assert row.status == CoachSuggestion.Status.PENDING

    def test_post_re_enqueues_a_row_stuck_in_flight(self, mock_task, auth_client, user):
        """An in-flight row is skipped by every later `get_or_create`, so an explicit
        click has to break the lock rather than wait for the scheduler's timeout."""
        _make_game(user)
        row = _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.RUNNING, attempts=3
        )

        response = auth_client.post("/game/944768131/analyze", {"sel": "3"})

        assert response.status_code == 200
        mock_task.delay.assert_called_once()
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.PENDING
        assert row.attempts == 0  # the user's retry isn't spent by earlier failures
        assert CoachSuggestion.objects.filter(user=user, game_id="944768131").count() == 1

    def test_post_re_analyses_a_failed_row(self, mock_task, auth_client, user):
        """A retired position must not be a dead end."""
        _make_game(user)
        row = _make_suggestion(
            user,
            _ply_fen(2),
            status=CoachSuggestion.Status.FAILED,
            eval_cp=None,
            best_move_san=None,
            best_move_uci=None,
            analysis="did not complete",
        )

        auth_client.post("/game/944768131/analyze", {"sel": "3"})

        mock_task.delay.assert_called_once()
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.PENDING
        assert row.analysis == ""

    def test_post_is_noop_for_opponent_move(self, mock_task, auth_client, user):
        _make_game(user)

        # sel 2 is Black's move (e5) — the coach only analyses the user's moves.
        response = auth_client.post("/game/944768131/analyze", {"sel": "2"})

        assert response.status_code == 200
        mock_task.delay.assert_not_called()
        assert CoachSuggestion.objects.count() == 0

    def test_get_returns_the_done_card(self, mock_task, auth_client, user):
        _make_game(user)
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
        _make_game(user)
        # sel 3 is White's 2nd move (Nf3); the coach would have played Bb5.
        _make_suggestion(
            user,
            _ply_fen(2),
            eval_text="+0.4",
            eval_cp=0.4,
            best_move_san="Bb5",
            best_move_uci="f1b5",
            analysis="Pin the knight.",
        )

        response = auth_client.get("/game/944768131/analyze", {"sel": "3"})
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
        _make_game(user)
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
        _make_game(user)

        response = auth_client.post("/game/944768131/analyze", {"sel": "3"})
        body = response.content.decode()

        # Enqueueing marks the move pending in the grid straight away; nothing
        # is done yet, so the history stays empty.
        assert 'id="gr-moves-panel" hx-swap-oob="true"' in body
        assert "gr-badge--pending" in body
        assert 'id="gr-history" hx-swap-oob="true"' in body
        assert 'class="gr-card__count">0<' in body

    def test_analysis_never_calls_chess_com(self, mock_task, auth_client, user):
        _make_game(user)

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
        _make_game(user)

        # sel 2 is Black's move (e5) — the opponent's.
        response = auth_client.get("/game/944768131/view", {"sel": "2"})

        assert response.context["coach"]["mode"] == "opponent"
        assert b"The coach only analyses your moves" in response.content

    def test_unanalyzed_shows_request_button(self, auth_client, user):
        _make_game(user)

        # sel 3 is White's Nf3 (a user move) with no suggestion yet.
        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "unanalyzed"
        assert "Request suggestion" in body
        assert 'hx-post="/game/944768131/analyze?sel=3"' in body
        assert 'hx-target="#gr-coach"' in body

    def test_pending_self_polls(self, auth_client, user):
        _make_game(user)
        _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "pending"
        assert "spinner" in body
        assert 'hx-trigger="every 2s"' in body
        assert 'hx-get="/game/944768131/analyze?sel=3"' in body

    def test_running_renders_as_pending(self, auth_client, user):
        """RUNNING and PENDING are worth telling apart in the scheduler (only a
        RUNNING analysis can time out) but not on the card: either way the answer
        isn't there yet."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.RUNNING)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["coach"]["mode"] == "pending"
        assert 'hx-trigger="every 2s"' in response.content.decode()

    def test_pending_does_not_offer_a_retry(self, auth_client, user):
        """The scheduler's timeout recovers a stuck analysis on its own; a button
        here would only invite breaking a lock on work that is still running."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.PENDING)

        body = auth_client.get("/game/944768131/view", {"sel": "3"}).content.decode()

        assert "Retry" not in body

    def test_failed_analysis_offers_a_retry(self, auth_client, user):
        _make_game(user)
        _make_suggestion(
            user,
            _ply_fen(2),
            status=CoachSuggestion.Status.FAILED,
            eval_cp=None,
            best_move_san=None,
            best_move_uci=None,
            analysis="Error during Stockfish analysis: boom",
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "failed"
        assert "t analyse" in body  # "The coach couldn&rsquo;t analyse Nf3."
        assert "Error during Stockfish analysis: boom" in body
        assert "Try again" in body
        # A failure is not a suggestion: it must not pad the analysis history.
        assert response.context["history_count"] == 0

    def test_a_retired_row_explains_itself(self, auth_client, user):
        """The scheduler retires a position without any prose — it never got far
        enough to produce any — so the card has to supply the reason."""
        _make_game(user)
        _make_suggestion(
            user,
            _ply_fen(2),
            status=CoachSuggestion.Status.FAILED,
            eval_cp=None,
            best_move_san=None,
            best_move_uci=None,
            eval_text="",
            analysis="",
        )

        body = auth_client.get("/game/944768131/view", {"sel": "3"}).content.decode()

        assert "did not complete" in body
        assert "Try again" in body

    def test_terminal_position_is_not_treated_as_a_failure(self, auth_client, user):
        """Stockfish has no move to suggest at mate/stalemate, but it still scores
        the position — that's an evaluation, not a failed analysis."""
        _make_game(user)
        _make_suggestion(
            user,
            _ply_fen(2),
            eval_cp=-10.0,
            best_move_san=None,
            best_move_uci=None,
            eval_text="Decisive advantage for Black: Mate in 2 moves.",
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["coach"]["mode"] == "analyzed"

    def test_analyzed_followed(self, auth_client, user):
        _make_game(user)
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
        _make_game(user)
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

    def test_last_ply_of_the_game_is_reviewed_like_any_other(
        self, auth_client, user
    ):
        # A game whose final ply is one of the user's own moves: it gets the full
        # review treatment — coach comparison and both board arrows — rather than
        # any special end-of-timeline state.
        last_pgn = '[Event "Test"]\n\n1. e4 e5 2. Nf3 *'
        _make_game(
            user,
            pgn=last_pgn,
            fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
        )
        # Suggestion for that move (position before White's 2. Nf3); best move
        # differs from the played Nf3 → both recommended and played arrows.
        fen_before = board_utils.moves_from_pgn(last_pgn)[2]["fen_before"]
        _make_suggestion(
            user, fen_before, best_move_san="d4", best_move_uci="d2d4"
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "analyzed"
        assert 'stroke="#b78e54"' in body  # brass recommended arrow
        assert 'stroke="#4a7a52"' in body  # green played-move arrow


@pytest.mark.django_db
class TestMovesGrid:
    """partials/moves_grid.html — the click-to-review move list."""

    def test_each_move_links_to_its_position(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert 'hx-get="/game/944768131/view?sel=1"' in body
        assert 'hx-target="#gr-view"' in body
        # The selected ply is flagged.
        assert "gr-move--sel" in body

    def test_analyzed_move_shows_followed_badge(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3")

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert "gr-move--analyzed" in body
        assert "gr-badge--followed" in body

    def test_pending_move_shows_pending_badge(self, auth_client, user):
        _make_game(user)
        _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert b"gr-badge--pending" in response.content


@pytest.mark.django_db
class TestNoFutureMoveSuggestion:
    """The coach never suggests a move that has not been played.

    ``FEN_LIVE`` is the position reached after the 4 plies of ``PGN`` — the one
    the user would play next. It never appears in the PGN as a played move, so a
    ``CoachSuggestion`` row stored against it (the app used to create one, and
    those rows are still in the database) must stay invisible everywhere.
    """

    def test_timeline_ends_on_the_last_played_move(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "5"})
        body = response.content.decode()

        # The cursor is clamped to the last ply; there is no slot past it.
        assert response.context["sel"] == response.context["head"] == 4
        assert response.context["next_sel"] == 4
        assert ">your move<" not in body  # the old provisional grid slot
        assert "gr-move--live" not in body

    def test_a_stored_suggestion_for_an_unplayed_position_is_not_shown(
        self, auth_client, user
    ):
        """The anti-regression that matters: those rows are still in the DB."""
        _make_game(user)
        _make_suggestion(user, FEN_LIVE, best_move_san="Bb5", best_move_uci="f1b5")

        response = auth_client.get("/game/944768131/view", {"sel": "4"})
        body = response.content.decode()

        assert "Bb5" not in body  # not in the card, the grid or the history
        assert "BEST MOVE" not in body
        assert 'stroke="#b78e54"' not in body  # no recommended-move arrow
        assert response.context["history_count"] == 0

    def test_a_pending_analysis_of_an_unplayed_position_is_not_shown(
        self, auth_client, user
    ):
        _make_game(user)
        _make_suggestion(user, FEN_LIVE, status=CoachSuggestion.Status.PENDING)

        response = auth_client.get("/game/944768131/view", {"sel": "4"})
        body = response.content.decode()

        assert "gr-badge--pending" not in body
        assert 'hx-trigger="every 2s"' not in body

    def test_the_card_at_the_end_reviews_the_last_move_played(
        self, auth_client, user
    ):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "4"})
        body = response.content.decode()

        # Ply 4 is Black's Nc6 — the opponent's, so the coach says as much.
        assert response.context["coach"]["mode"] == "opponent"
        assert "Reviewing: 2… Nc6" in body

    def test_a_move_once_played_does_get_its_suggestion(self, auth_client, user):
        """The other half of the rule: a played move is analysed and shown."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), best_move_san="d4", best_move_uci="d2d4")

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "analyzed"
        assert "The coach preferred" in body
        assert "d4" in body


@pytest.mark.django_db
class TestHistoryList:
    """partials/history_list.html — the analysed-moves timeline."""

    def test_counts_and_lists_analysed_user_moves(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3")  # followed

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["history_count"] == 1
        assert 'class="gr-card__count">1<' in body
        assert 'hx-get="/game/944768131/view?sel=3"' in body
        assert "gr-tag--followed" in body

    def test_empty_when_no_analysis(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "1"})

        assert response.context["history_count"] == 0
        assert b"gr-tag--" not in response.content


@pytest.mark.django_db
class TestBoardRendering:
    """partials/board.html expanded from the stored FEN."""

    def test_renders_64_cells(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "0"})

        assert len(response.context["cells"]) == 64

    def test_last_move_highlights_two_squares(self, auth_client, user):
        _make_game(user)

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
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()
        ctx = response.context

        assert (ctx["prev_sel"], ctx["next_sel"], ctx["head"]) == (2, 4, 4)
        assert 'hx-get="/game/944768131/view?sel=0"' in body  # start
        assert 'hx-get="/game/944768131/view?sel=2"' in body  # back
        assert 'hx-get="/game/944768131/view?sel=4"' in body  # forward / end

    def test_end_button_targets_the_last_played_ply(self, auth_client, user):
        """"End" goes to `head`; there is no live position to jump to."""
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "2"})
        body = response.content.decode()

        assert 'hx-get="/game/944768131/view?sel=4"' in body
        assert 'hx-get="/game/944768131/view?sel=5"' not in body
        assert "gr-live-btn" not in body


@pytest.mark.django_db
class TestGameListStates:
    """partials/game_list.html — the empty state and the review card."""

    def test_empty_state(self, auth_client):
        response = auth_client.get("/games")

        assert b"No games to review" in response.content

    def test_review_card(self, auth_client, user):
        _make_game(user, fen=FEN_START)

        response = auth_client.get("/games")
        body = response.content.decode()

        assert "gm-card--past" in body
        assert "REVIEW" in body
        # Nothing that would advertise a game in progress.
        assert "LIVE" not in body
        assert "Your turn" not in body


@pytest.mark.django_db
class TestEvalBar:
    """The eval bar fill (_eval_fill / partials/_evalfill.html)."""

    def test_defaults_to_midpoint_without_analysis(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["eval_fill"] == 50
        assert b"height:50%" in response.content

    def test_clamps_a_large_positive_eval(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), best_move_san="Nf3", eval_cp=10)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["eval_fill"] == 93
        assert b"height:93%" in response.content

    def test_clamps_a_large_negative_eval(self, auth_client, user):
        _make_game(user)
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
