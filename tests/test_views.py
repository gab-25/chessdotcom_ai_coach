"""Unit tests for the views.

The app shows finished games only, so ``_make_game`` builds one by default
(``is_active=False``); the few tests that need a game still in progress pass
``is_active=True`` and assert it is refused. The detail page reads entirely from
the stored ``Game`` snapshot and ``CoachSuggestion`` rows — no Chess.com call —
so these tests just seed the DB. The Celery task is mocked where analysis is
enqueued.

The ``user`` fixture links no Chess.com account, so `sync.request_sync` returns
early and no view here reaches the broker — which matters for ``/games``, the one
endpoint that still calls it. The other exception is the detail page load, which
runs the recovery sweeps — patched out in `_no_recovery_sweep`.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.utils import timezone

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.views import GAMES_PER_PAGE
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


@pytest.fixture(autouse=True)
def _no_recovery_sweep():
    """Loading the detail page runs the recovery sweeps, which read the broker.

    They have their own tests in `test_sync.py`; here they would only add a
    connection attempt. The mock is handed to the three tests that pin *when* the
    sweeps run, which is the whole of what the view decides.
    """
    with patch("chessdotcom_ai_coach.views.sync.recover_stuck_analyses") as mock:
        mock.return_value = 0
        yield mock


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

    def test_has_sync_button(self, auth_client, user):
        """The button, not the "All" filter chip: both point at /games, so this
        asserts on the id the head control carries and the chips do not."""
        _make_game(user)

        response = auth_client.get("/")

        assert b'id="game-list"' in response.content
        assert b'hx-target="#game-list"' in response.content
        assert response.content.count(b'id="home-sync"') == 1
        assert b'hx-get="/games?time_class="' in response.content

    def test_the_home_page_neither_polls_nor_refreshes_itself(self, auth_client, user):
        """The grid used to refresh itself once when a page load claimed the sync.
        A page load claims nothing now, so the home page carries no timed trigger
        of any kind — neither a poll nor a one-shot."""
        _make_game(user)

        response = auth_client.get("/")

        assert b"every " not in response.content
        assert b"load delay:6s" not in response.content
        assert b"AUTO-REFRESH" not in response.content

    def test_the_home_page_does_not_start_a_sync(self, auth_client, user):
        """The home page is a plain DB read. The Sync button is what fetches."""
        _make_game(user)

        with patch("chessdotcom_ai_coach.views.sync.request_sync") as mock_sync:
            auth_client.get("/")

        mock_sync.assert_not_called()

    def test_shows_game_count_once(self, auth_client, user):
        _make_game(user)

        response = auth_client.get("/")

        assert response.content.count(b'id="home-count"') == 1
        assert b"of 1 finished game" in response.content
        assert b"hx-swap-oob" not in response.content

    def test_does_not_list_a_game_in_progress(self, auth_client, user):
        """A game still being played has nothing to review, so it isn't shown."""
        _make_game(user, is_active=True)

        response = auth_client.get("/")

        assert list(response.context["games"]) == []
        assert b"944768131" not in response.content
        assert b"No games to review" in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.sync_user_task")
class TestGameListSync:
    """Pressing Sync is what asks for the archive to be imported.

    There is no scheduler and the home page starts nothing, so this endpoint
    carries the only trigger — rate-limited by the per-user claim, and visible to
    the user as a single delayed re-fetch of the fragment once the games have had
    a moment to land.
    """

    @pytest.fixture
    def linked_client(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username="MyUser"
        )
        client.force_login(user)
        return client, user

    def test_pressing_refresh_queues_the_import_and_re_fetches_once(
        self, mock_task, linked_client
    ):
        client, _user = linked_client

        response = client.get("/games")

        mock_task.apply_async.assert_called_once()
        assert b"load delay:6s" in response.content
        assert response.content.count(b"load delay:6s") == 1

    def test_opening_the_home_page_queues_nothing(self, mock_task, linked_client):
        """The inversion of the test above: a page load is a pure DB read, so it
        does not even take the claim."""
        client, user = linked_client

        response = client.get("/")

        mock_task.apply_async.assert_not_called()
        user.refresh_from_db()
        assert user.last_synced_at is None
        assert b"load delay:6s" not in response.content

    def test_a_second_refresh_within_the_cooldown_neither_queues_nor_re_fetches(
        self, mock_task, linked_client
    ):
        client, _user = linked_client
        client.get("/games")
        mock_task.apply_async.reset_mock()

        response = client.get("/games")

        mock_task.apply_async.assert_not_called()
        assert b"load delay:6s" not in response.content

    def test_the_re_fetch_six_seconds_later_asks_for_nothing_further(
        self, mock_task, linked_client
    ):
        """The chain is two requests long by construction: the re-fetch names
        itself with `after_sync`, and a request carrying it never claims, so the
        fragment it gets back cannot contain another one. Without this the
        invariant would rest on SYNC_COOLDOWN merely being longer than 6s."""
        client, _user = linked_client
        first = client.get("/games")
        assert b"load delay:6s" in first.content
        mock_task.apply_async.reset_mock()

        second = client.get("/games", {"after_sync": "1"})

        mock_task.apply_async.assert_not_called()
        assert b"load delay:6s" not in second.content

    def test_the_re_fetch_keeps_the_filter_and_the_page(self, mock_task, linked_client):
        """Its job is to reproduce the view already on screen, unlike the Sync
        button, which means "show me the newest" and so drops the page."""
        client, user = linked_client
        for i in range(GAMES_PER_PAGE + 1):
            _make_game(user, game_id=f"g{i}", time_class="rapid")

        response = client.get("/games", {"page": "2", "time_class": "rapid"})

        assert b"load delay:6s" in response.content
        assert b"after_sync=1&amp;page=2&amp;time_class=rapid" in response.content

    def test_an_unlinked_user_queues_nothing(self, mock_task, auth_client, user):
        """The default `user` fixture has no Chess.com account."""
        response = auth_client.get("/games")

        mock_task.apply_async.assert_not_called()
        assert b"load delay:6s" not in response.content

    def test_the_button_reports_the_import_for_as_long_as_it_runs(
        self, mock_task, linked_client
    ):
        """The button is the only thing that says an import is running, so the
        state cannot ride on htmx's own in-flight class: this endpoint is a DB
        read that answers in about two milliseconds, while the import it queued
        runs in the worker for seconds. `sync_started` is what carries it — from
        the press to the re-fetch six seconds later."""
        client, _user = linked_client

        response = client.get("/games")

        assert b"btn--sm is-syncing" in response.content
        assert b'hx-swap="innerHTML" disabled aria-busy="true"' in response.content

    def test_the_button_leaves_that_state_on_the_re_fetch(
        self, mock_task, linked_client
    ):
        """The re-fetch never claims, so the button it swaps back in is idle — it
        is what ends the running state, which otherwise would have nothing to
        end it."""
        client, _user = linked_client
        client.get("/games")

        response = client.get("/games", {"after_sync": "1"})

        assert b"is-syncing" not in response.content
        assert b"aria-busy" not in response.content

    def test_the_button_is_idle_when_no_import_was_queued(
        self, mock_task, linked_client
    ):
        """Running is true only of a request that took the claim. A page load
        takes none, and a second Sync inside the cooldown takes none."""
        client, _user = linked_client

        assert b"is-syncing" not in client.get("/").content
        client.get("/games")
        assert b"is-syncing" not in client.get("/games").content

    def test_the_empty_state_leaves_a_running_import_to_the_button(
        self, mock_task, linked_client
    ):
        """The grid says only that it is empty; the button says why. Two places
        reporting the same import is what this avoids, so the message is the same
        under a claimed sync as inside the cooldown that claims nothing."""
        client, _user = linked_client

        running = client.get("/games").content
        idle = client.get("/games").content

        assert b"No games to review" in running
        assert b"None of your Chess.com games are here yet." in running
        assert idle.count(b"None of your Chess.com games are here yet.") == 1

    def test_the_empty_state_reads_the_same_for_an_unlinked_user(
        self, mock_task, auth_client, user
    ):
        """An unlinked account has no archive to import, but that is the Sync
        button's story too: the grid still says only that it holds nothing."""
        content = auth_client.get("/").content

        assert b"No games to review" in content
        assert b"None of your Chess.com games are here yet." in content

    def test_a_broker_outage_still_renders_the_games_fragment(
        self, mock_task, linked_client
    ):
        """Nothing in a request may depend on Redis being up."""
        client, _user = linked_client
        mock_task.apply_async.side_effect = RuntimeError("broker down")

        response = client.get("/games")

        assert response.status_code == 200
        assert b"load delay:6s" not in response.content


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
        assert b"of 1 finished game" in response.content

    def test_swaps_the_sync_button_back_in_with_the_active_filter(
        self, auth_client, user
    ):
        """The button lives outside #game-list, so without this its time_class
        would stay frozen at whatever the page was first loaded with and pressing
        Sync would silently drop the filter. Asserted as one string because the
        "rapid" filter chip renders the same hx-get."""
        _make_game(user)

        response = auth_client.get("/games", {"time_class": "rapid"})

        assert (
            b'id="home-sync" hx-swap-oob="true" hx-get="/games?time_class=rapid"'
            in response.content
        )

    def test_the_re_fetch_is_absent_when_nothing_was_queued(self, auth_client, user):
        """The `user` fixture links no account, so the fragment claims nothing."""
        _make_game(user)

        response = auth_client.get("/games")

        assert b"load delay:6s" not in response.content


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
        """Not one `hx-trigger="every …"` anywhere on the page.

        Progress used to arrive on two timers — the coach card's and the analysis
        block's — and now arrives when the user reloads the page. Matching the
        bare `every ` is what keeps a third one from being added quietly."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.PENDING)

        response = auth_client.get("/game/944768131")

        assert b"every " not in response.content
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

    def test_nothing_is_swapped_out_of_band(self, auth_client, user):
        """One render, one target.

        The eval bar, the arrows, the history and the moves grid used to be
        swapped out-of-band by the coach card's poll, each needing an id of its
        own to be aimed at. They are plain parts of #gr-view now, so any
        `hx-swap-oob` here would mean that plumbing has grown back."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2))

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert "hx-swap-oob" not in body
        assert body.count('id="gr-view"') == 1

    def test_there_is_no_refresh_button(self, auth_client, user):
        """The page is a snapshot of the database as of the load, and reloading
        is how a newer one is taken — so nothing on it re-reads in place. The
        button this replaces was the last control that did."""
        _make_game(user)
        states = (
            [],
            [(_ply_fen(0), CoachSuggestion.Status.PENDING)],
            [
                (_ply_fen(0), CoachSuggestion.Status.DONE),
                (_ply_fen(2), CoachSuggestion.Status.DONE),
            ],
        )
        for rows in states:
            CoachSuggestion.objects.all().delete()
            for fen, status in rows:
                _make_suggestion(user, fen, status=status)

            body = auth_client.get("/game/944768131").content.decode()

            assert 'id="gr-refresh"' not in body, rows
            assert "refresh=1" not in body, rows
            assert "Refresh" not in body, rows

    def test_loading_the_page_runs_the_recovery_sweeps(
        self, auth_client, user, _no_recovery_sweep
    ):
        """The page load is the one request that means "where did the analysis
        get to", so it is where the stuck-analysis sweeps live now that the
        Refresh button that used to carry them is gone."""
        _make_game(user)

        auth_client.get("/game/944768131")

        _no_recovery_sweep.assert_called_once_with(user)

    def test_navigation_does_not_run_the_recovery_sweeps(
        self, auth_client, user, _no_recovery_sweep
    ):
        """The sweeps read the broker's queue depth; the arrow keys must not."""
        _make_game(user)

        auth_client.get("/game/944768131/view", {"sel": "3"})

        _no_recovery_sweep.assert_not_called()

    def test_a_game_we_refuse_to_review_runs_no_sweeps(
        self, auth_client, user, _no_recovery_sweep
    ):
        """The sweeps hang off the 404 check, not in front of it: a page that
        refuses the game is not somebody asking after an analysis."""
        _make_game(user, is_active=True)

        assert auth_client.get("/game/944768131").status_code == 404

        _no_recovery_sweep.assert_not_called()

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

    def test_unanalyzed_points_at_the_whole_game_control(self, auth_client, user):
        """The card asks for nothing itself: analysis is requested once, for the
        whole game, by the block below it in the same column."""
        _make_game(user)

        # sel 3 is White's Nf3 (a user move) with no suggestion yet.
        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "unanalyzed"
        assert "No suggestion for <b>Nf3</b> yet" in body
        assert "Analyse this game" in body

    def test_analyse_button_scrolls_back_to_the_top(self, auth_client, user):
        """The button sits below the board but replaces the view above it.

        Without the swap modifier the replaced content lands off-screen and the
        press reads as having done nothing. The move navigation swaps the same
        target and deliberately does not scroll.
        """
        _make_game(user)

        body = auth_client.get("/game/944768131/view", {"sel": "3"}).content.decode()

        assert 'hx-swap="outerHTML show:window:top"' in body
        assert body.count("show:window:top") == 1  # only the analyse control
        assert 'hx-swap="outerHTML"' in body  # navigation stays where it is

    def test_pending_waits_to_be_reloaded(self, auth_client, user):
        """It used to promise the suggestion would "appear shortly", which a page
        that no longer polls cannot keep: it says what to do instead."""
        _make_game(user)
        _make_suggestion(
            user, _ply_fen(2), status=CoachSuggestion.Status.PENDING
        )

        response = auth_client.get("/game/944768131/view", {"sel": "3"})
        body = response.content.decode()

        assert response.context["coach"]["mode"] == "pending"
        assert "spinner" in body
        assert "Reload the page" in body
        assert "every " not in body

    def test_running_renders_as_pending(self, auth_client, user):
        """RUNNING and PENDING are worth telling apart to the sweeps (only a
        RUNNING analysis can time out) but not on the card: either way the answer
        isn't there yet."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.RUNNING)

        response = auth_client.get("/game/944768131/view", {"sel": "3"})

        assert response.context["coach"]["mode"] == "pending"
        assert "Analysing <b>Nf3</b>" in response.content.decode()

    def test_pending_does_not_offer_a_retry(self, auth_client, user):
        """The recovery sweep unsticks an analysis on its own, and the page load
        is what runs it — nothing here invites breaking a lock on work still
        running."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.PENDING)

        body = auth_client.get("/game/944768131/view", {"sel": "3"}).content.decode()

        assert "Retry" not in body

    def test_failed_analysis_explains_itself(self, auth_client, user):
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
        # No retry of its own: the whole-game button re-queues a FAILED row
        # without `force`, and a failed move is what keeps that button on screen.
        assert "Analyse this game" in body
        # A failure is not a suggestion: it must not pad the analysis history.
        assert response.context["history_count"] == 0

    def test_a_retired_row_explains_itself(self, auth_client, user):
        """The recovery sweep retires a position without any prose — it never got far
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
        assert "Analyse this game" in body

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
        # It has no ply to attach to, so it must not make the game look busy.
        assert response.context["analysis_pending"] == 0
        assert "Analysis in progress" not in body

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
class TestHomePaging:
    """An imported archive is thousands of rows, so the grid is paged and filtered."""

    def _make_games(self, user, count, **overrides):
        now = timezone.now()
        for i in range(count):
            _make_game(
                user,
                game_id=f"g{i:03d}",
                end_time=now - timedelta(days=i),
                **overrides,
            )

    def test_first_page_is_capped(self, auth_client, user):
        self._make_games(user, GAMES_PER_PAGE + 5)

        response = auth_client.get("/")

        assert len(response.context["games"]) == GAMES_PER_PAGE
        assert response.context["total"] == GAMES_PER_PAGE + 5

    def test_second_page_holds_the_remainder(self, auth_client, user):
        self._make_games(user, GAMES_PER_PAGE + 5)

        response = auth_client.get("/games", {"page": "2"})

        assert len(response.context["games"]) == 5
        assert response.context["page"].number == 2

    def test_an_out_of_range_page_lands_on_a_real_one(self, auth_client, user):
        """The page number is in a URL, so junk must not 500."""
        self._make_games(user, 3)

        assert auth_client.get("/games", {"page": "99"}).status_code == 200
        assert auth_client.get("/games", {"page": "nope"}).status_code == 200

    def test_newest_game_first(self, auth_client, user):
        now = timezone.now()
        _make_game(user, game_id="older", end_time=now - timedelta(days=2))
        _make_game(user, game_id="newer", end_time=now)

        response = auth_client.get("/")

        assert [g.game_id for g in response.context["games"]] == ["newer", "older"]

    def test_filters_by_time_class(self, auth_client, user):
        _make_game(user, game_id="b", time_class="blitz")
        _make_game(user, game_id="d", time_class="daily")

        response = auth_client.get("/games", {"time_class": "blitz"})

        assert [g.game_id for g in response.context["games"]] == ["b"]
        assert response.context["time_class"] == "blitz"

    def test_offers_only_the_time_classes_present(self, auth_client, user):
        _make_game(user, game_id="b", time_class="blitz")
        _make_game(user, game_id="d", time_class="daily")

        response = auth_client.get("/")

        assert response.context["time_classes"] == ["blitz", "daily"]

    def test_paging_keeps_the_filter(self, auth_client, user):
        """The pager and the filter must not cancel each other out."""
        self._make_games(user, GAMES_PER_PAGE + 2, time_class="blitz")
        _make_game(user, game_id="daily1", time_class="daily")

        response = auth_client.get("/games", {"time_class": "blitz"})
        body = response.content.decode()

        assert "page=2&amp;time_class=blitz" in body

    def test_empty_filter_result_says_so(self, auth_client, user):
        _make_game(user, game_id="b", time_class="blitz")

        response = auth_client.get("/games", {"time_class": "daily"})

        assert b"No finished daily games" in response.content


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
class TestAnalyzeWholeGame:
    """Analysis is on demand: nothing queues a game until the user asks."""

    def test_post_queues_every_move_the_user_played(self, mock_task, auth_client, user):
        _make_game(user)

        response = auth_client.post("/game/944768131/analyze-game")

        assert response.status_code == 200
        # PGN is 1. e4 e5 2. Nf3 Nc6 — the user is White, so two moves.
        assert mock_task.delay.call_count == 2
        assert CoachSuggestion.objects.filter(user=user).count() == 2

    def test_post_twice_queues_nothing_extra(self, mock_task, auth_client, user):
        _make_game(user)

        auth_client.post("/game/944768131/analyze-game")
        auth_client.post("/game/944768131/analyze-game")

        assert mock_task.delay.call_count == 2  # idempotent: a double click is free

    def test_refuses_a_game_in_progress(self, mock_task, auth_client, user):
        _make_game(user, is_active=True)

        response = auth_client.post("/game/944768131/analyze-game")

        assert response.status_code == 404
        mock_task.delay.assert_not_called()

    def test_a_part_analysed_game_still_offers_the_button(
        self, mock_task, auth_client, user
    ):
        """The counts stay in the context — they pick the branch — but the page
        says only which state it is in."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(0))

        response = auth_client.get("/game/944768131")
        body = response.content.decode()

        assert response.context["analysis_total"] == 2
        assert response.context["analysis_done"] == 1
        assert response.context["analysis_complete"] is False
        assert "Not analysed yet" in body
        assert "Analyse this game" in body

    def test_an_analysis_in_progress_is_shown_without_counters(
        self, mock_task, auth_client, user
    ):
        """A count that moves only when you press a button reads as a stalled
        count, so the page reports the state and nothing else."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(0))
        _make_suggestion(user, _ply_fen(2), status=CoachSuggestion.Status.PENDING)

        response = auth_client.get("/game/944768131")
        body = response.content.decode()

        assert response.context["analysis_pending"] == 1
        assert "Analysis in progress" in body
        assert "spinner" in body
        assert "of 2" not in body
        assert "queued" not in body
        # No control to press while the queue drains — pressing it would queue
        # nothing anyway, since every ply already has a row. Reloading is the move.
        assert "Analyse this game" not in body
        assert "Analysing&hellip;" in body

    def test_a_complete_analysis_is_shown_without_counters(
        self, mock_task, auth_client, user
    ):
        _make_game(user)
        _make_suggestion(user, _ply_fen(0))
        _make_suggestion(user, _ply_fen(2))

        body = auth_client.get("/game/944768131").content.decode()

        assert "Analysis complete" in body
        assert "of 2" not in body

    def test_a_failed_move_is_re_queued_without_force(
        self, mock_task, auth_client, user
    ):
        """The per-move retry is gone, so the plain button has to be the way back:
        a failed ply keeps the game off "complete", which is what leaves that
        button — rather than the forced re-run — on the page."""
        _make_game(user)
        done = _make_suggestion(user, _ply_fen(0), analysis="a good line")
        failed = _make_suggestion(
            user,
            _ply_fen(2),
            status=CoachSuggestion.Status.FAILED,
            eval_cp=None,
            best_move_san=None,
            best_move_uci=None,
            analysis="boom",
        )

        response = auth_client.post("/game/944768131/analyze-game")

        assert response.status_code == 200
        assert mock_task.delay.call_count == 1
        failed.refresh_from_db()
        assert failed.status == CoachSuggestion.Status.PENDING
        assert failed.attempts == 0
        assert failed.analysis == ""
        # And the good analysis beside it is untouched: this is a retry, not a
        # re-run of the whole game.
        done.refresh_from_db()
        assert done.status == CoachSuggestion.Status.DONE
        assert done.analysis == "a good line"

    def test_analysis_never_calls_chess_com(self, mock_task, auth_client, user):
        """Everything the analysis needs is in the stored game."""
        _make_game(user)

        with patch("chessdotcom_ai_coach.services.chess_client.Client") as mock_client:
            auth_client.post("/game/944768131/analyze-game")

        mock_client.assert_not_called()

    def test_a_finished_game_offers_a_re_analysis(self, mock_task, auth_client, user):
        """A fully analysed game is not a dead end: the button becomes a re-run."""
        _make_game(user)
        _make_suggestion(user, _ply_fen(0))
        _make_suggestion(user, _ply_fen(2))

        response = auth_client.get("/game/944768131")

        assert response.context["analysis_complete"] is True
        assert b"Analysis complete" in response.content
        assert b"Re-analyse this game" in response.content
        assert b"force=1" in response.content

    def test_force_re_queues_a_fully_analysed_game(self, mock_task, auth_client, user):
        _make_game(user)
        rows = [_make_suggestion(user, _ply_fen(0)), _make_suggestion(user, _ply_fen(2))]

        response = auth_client.post("/game/944768131/analyze-game?force=1")

        assert response.status_code == 200
        assert mock_task.delay.call_count == 2
        # Re-analysed in place: no second row for a ply that already had one.
        assert CoachSuggestion.objects.filter(user=user).count() == 2
        for row in rows:
            row.refresh_from_db()
            assert row.status == CoachSuggestion.Status.PENDING
            assert row.analysis == ""

    def test_without_force_a_fully_analysed_game_queues_nothing(
        self, mock_task, auth_client, user
    ):
        _make_game(user)
        _make_suggestion(user, _ply_fen(0))
        _make_suggestion(user, _ply_fen(2))

        auth_client.post("/game/944768131/analyze-game")

        mock_task.delay.assert_not_called()


@pytest.mark.django_db
class TestTemplateSyntaxNeverLeaks:
    """No raw template markup in a rendered page.

    Django's `{# ... #}` is single-line only: spread it over two lines and the
    whole thing is emitted as text instead of being stripped. That reads as a
    normal comment in the source, so it is caught here rather than by eye.
    """

    LEAKS = ("{#", "{%", "{{")

    def _assert_clean(self, response):
        body = response.content.decode()
        for marker in self.LEAKS:
            assert marker not in body, f"unrendered template markup {marker!r} in output"

    def test_home_is_clean(self, auth_client, user):
        _make_game(user)

        self._assert_clean(auth_client.get("/"))

    def test_empty_home_is_clean(self, auth_client):
        self._assert_clean(auth_client.get("/"))

    def test_game_list_fragment_is_clean(self, auth_client, user):
        _make_game(user)

        self._assert_clean(auth_client.get("/games"))

    def test_detail_page_is_clean(self, auth_client, user):
        _make_game(user)
        _make_suggestion(user, _ply_fen(2))

        self._assert_clean(auth_client.get("/game/944768131"))
        self._assert_clean(auth_client.get("/game/944768131/view", {"sel": "3"}))

    def test_error_page_is_clean(self, auth_client, user):
        _make_game(user, is_active=True)

        self._assert_clean(auth_client.get("/game/944768131"))


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
