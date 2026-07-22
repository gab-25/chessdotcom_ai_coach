from asgiref.sync import sync_to_async
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from .models import CoachSuggestion
from .services import board as board_utils
from .services import game_store
from .services.chess_client import Client
from .tasks import analyze_game_task


def _client_for(user) -> Client:
    """Build a Chess.com client for the authenticated user."""
    return Client(username=user.chess_username)


def _decorate_games(games, username):
    """Attach board cells, move number, side-to-move and turn ownership to `Game` rows.

    Used for both the current and past-games sections of the home page (both
    now plain DB reads — see `home`/`game_list`). `turn` reproduces the
    side-to-move the Chess.com API used to supply directly, derived from the
    stored FEN, so the game card's "to move" tag keeps working. `is_user_turn`
    mirrors `_is_user_turn` so the list can highlight games awaiting the
    logged-in user's move.
    """
    for game in games:
        game.cells = board_utils.fen_to_cells(game.fen)
        game.move_no = board_utils.fullmove_number(game.fen)
        game.turn = board_utils.active_color(game.fen)
        orientation = "white" if game.white_name.lower() == username.lower() else "black"
        game.is_user_turn = game.turn == orientation
    return games


def _game_data_from_stored(stored):
    """Rebuild the ``game_data`` shape ``_build_detail_context`` expects from a Game.

    Lets the detail view fall back to the persisted snapshot when a game is no
    longer current on Chess.com.
    """
    return {
        "game": {
            "fen": stored.fen,
            "pgn": stored.pgn,
            "time_class": stored.time_class,
            "url": stored.url,
        },
        "white_name": stored.white_name or "White",
        "black_name": stored.black_name or "Black",
        "white_rating": stored.white_rating or None,
        "black_rating": stored.black_rating or None,
    }


def _uci_to_squares(uci):
    """Turn a UCI move like ``"d4f5"`` into its from/to square names."""
    if uci and len(uci) >= 4:
        return [uci[0:2], uci[2:4]]
    return []


def _suggestion_for_fen(history, fen):
    """Return ``(suggestion, highlight)`` for the stored analysis of ``fen``.

    Lets the detail render (and the poll) keep the coach panel populated for the
    position on the board until a new move changes the FEN. ``(None, None)`` when
    no analysis exists for the current position.
    """
    for row in history:
        if row.fen == fen:
            suggestion = {
                "eval_cp": row.eval_cp,
                "eval_text": row.eval_text,
                "best_move_san": row.best_move_san,
                "best_move_uci": row.best_move_uci,
                "analysis": row.analysis,
                "status": row.status,
            }
            return suggestion, _uci_to_squares(row.best_move_uci)
    return None, None


def _poll_token(fen, suggestion):
    """Poll cursor: changes when the position OR its analysis state changes.

    The hidden poller sends the token it currently reflects; ``game_detail`` swaps
    the body only when the fresh token differs. Encoding the suggestion status
    (not just the FEN) means a PENDING→DONE transition for the *same* position is
    picked up — otherwise the completed analysis would never replace the
    "Analyzing…" card until a manual page reload.
    """
    status = suggestion["status"] if suggestion else "-"
    return f"{fen}|{status}"


def _is_user_turn(game_data, username):
    """True when the side to move is the side the user plays."""
    orientation = (
        "white" if game_data["white_name"].lower() == username.lower() else "black"
    )
    return board_utils.active_color(game_data["game"].get("fen")) == orientation


def _eval_fill(eval_cp):
    """Map a White-POV centipawn eval to the eval bar's white fill percentage."""
    if eval_cp is None:
        return 50
    pct = 50 + eval_cp * 7
    return max(7, min(93, round(pct)))


def _build_detail_context(
    game_data,
    username,
    id,
    *,
    highlight=None,
    analysis=None,
    suggestion=None,
    history=None,
    can_analyze=True,
):
    """Assemble the game-detail template context from already-fetched data.

    Pure (no IO) so the async coach view can fetch once and rebuild cheaply. The
    played moves are derived from the PGN and annotated with the coach analysis
    (if any) requested at each position, joined by ``(move_no, color)``.
    """
    game = game_data["game"]
    fen = game.get("fen")
    pgn = game.get("pgn")
    white_name = game_data["white_name"]
    orientation = "white" if white_name.lower() == username.lower() else "black"

    history = history or []
    moves = board_utils.moves_from_pgn(pgn)
    board_utils.annotate_moves(moves, history)

    active = board_utils.active_color(fen)
    is_user_turn = active == orientation

    return {
        "id": id,
        "game": game,
        "white_name": white_name,
        "black_name": game_data["black_name"],
        "white_rating": game_data.get("white_rating"),
        "black_rating": game_data.get("black_rating"),
        "fen": fen,
        "pgn": pgn,
        "orientation": orientation,
        "cells": board_utils.fen_to_cells(
            fen, highlight=highlight, flipped=(orientation == "black")
        ),
        "move_no": board_utils.fullmove_number(fen),
        "turn_label": active.capitalize(),
        "is_user_turn": is_user_turn,
        "last_move": board_utils.last_move_from_pgn(pgn),
        "analysis": analysis,
        # The current position is queued/in-flight in the background pipeline: the
        # coach panel shows an "Analyzing…" state until the poll picks up the result.
        "analyzing": bool(
            suggestion and suggestion.get("status") == CoachSuggestion.Status.PENDING
        ),
        "eval_fill": _eval_fill(suggestion["eval_cp"]) if suggestion else 50,
        "best_move_san": suggestion["best_move_san"] if suggestion else None,
        "eval_text": suggestion["eval_text"] if suggestion else None,
        "moves": moves,
        "history": history,
        "can_analyze": can_analyze,
        "poll_token": _poll_token(fen, suggestion),
    }


def _fetch_detail(user, id):
    """Fetch a game and return ``(game_data, error_message)``."""
    game_data = _client_for(user).game_detail(id)
    if not game_data:
        return None, "Game not found or no longer active."
    return game_data, None


@login_required
def home(request):
    """Home page: lists the user's current games plus the past-games history.

    Plain DB read — the scheduler (`services.scheduler.sync_current_games`)
    is the only path that pulls fresh data from Chess.com into `Game`.
    """
    username = request.user.chess_username
    games = _decorate_games(game_store.current_games(request.user), username)
    past = _decorate_games(game_store.past_games(request.user), username)
    return render(request, "home.html", {"games": games, "past_games": past})


@login_required
def game_list(request):
    """HTMX endpoint: current games + past-games history fragment for polling.

    Plain DB read, same as `home` — the scheduler keeps `Game` fresh, so the
    5s poll here never has to touch Chess.com itself.
    """
    username = request.user.chess_username
    games = _decorate_games(game_store.current_games(request.user), username)
    past = _decorate_games(game_store.past_games(request.user), username)
    return render(
        request, "partials/game_list.html", {"games": games, "past_games": past}
    )


@login_required
def game_detail(request, id):
    """Game detail page: renders the board, moves and the AI-coach panel.

    Falls back to the persisted snapshot when the game is no longer current on
    Chess.com, so past games stay browsable (read-only: no re-analysis).
    """
    try:
        game_data, error = _fetch_detail(request.user, id)
    except Exception as exc:
        if request.htmx:
            # Transient fetch failure during a poll: skip this cycle, keep polling.
            return HttpResponse(status=204)
        return render(request, "error.html", {"message": str(exc)}, status=500)

    can_analyze = True
    if error:
        stored = game_store.stored_game(request.user, id)
        if stored is None:
            if request.htmx:
                return HttpResponse(status=286)  # nothing to poll for: stop the poll
            return render(request, "game.html", {"id": id, "error": error})
        game_data = _game_data_from_stored(stored)
        can_analyze = False  # past game: history is read-only

    fen = game_data["game"].get("fen")

    history = list(CoachSuggestion.objects.filter(user=request.user, game_id=id))
    suggestion, highlight = _suggestion_for_fen(history, fen)

    # HTMX poll (hidden poller inside #game-body) bookkeeping:
    if request.htmx:
        if not can_analyze:
            # Game is no longer live -> HTTP 286 tells HTMX to stop polling.
            return HttpResponse(status=286)
        if request.GET.get("since") == _poll_token(fen, suggestion):
            # Nothing changed — same position AND same analysis state -> no swap,
            # leave the DOM (and analysis) untouched.
            return HttpResponse(status=204)

    context = _build_detail_context(
        game_data,
        request.user.chess_username,
        id,
        highlight=highlight,
        analysis=suggestion["analysis"] if suggestion else None,
        suggestion=suggestion,
        history=history,
        can_analyze=can_analyze,
    )
    if request.htmx:
        # Position changed since the client's `since` -> swap the fresh body.
        return render(request, "partials/game_body.html", context)
    return render(request, "game.html", context)


@login_required
async def coach_suggestion(request, id):
    """HTMX endpoint: queues background analysis and re-renders the game body.

    Analysis no longer runs inline: when it's the user's turn this enqueues a
    Celery task (deduped per position) and swaps ``#game-body`` with the stored
    state — pending shows an "Analyzing…" card, and the existing poller reveals
    the recommended move and eval once the worker finishes.
    """
    # Async view: resolve the user via auser() to avoid a synchronous DB
    # access (request.user is lazy and would raise SynchronousOnlyOperation).
    user = await request.auser()
    game_data, error = _fetch_detail(user, id)
    if error:
        # Bare fragment (no base template) — the async context can't touch the
        # DB, and base.html's header reads request.user. Re-analysis is only
        # offered on active games, so a missing game is a genuine error here.
        return render(request, "partials/detail_error.html", {"error": error})

    game = game_data["game"]
    fen = game.get("fen", "")

    # When it's the user's turn, ensure the position is queued for background
    # analysis (idempotent): a pending/done row already exists for a FEN that's
    # queued or analysed, so we only enqueue a Celery task when we just created it.
    # A stale button click (opponent already moved) simply enqueues nothing.
    if _is_user_turn(game_data, user.chess_username):
        row, created = await CoachSuggestion.objects.aget_or_create(
            user=user,
            game_id=id,
            fen=fen,
            defaults={
                "status": CoachSuggestion.Status.PENDING,
                "move_no": board_utils.fullmove_number(fen),
                "eval_text": "",
                "analysis": "",
            },
        )
        # Enqueue on first request, and re-enqueue when re-analysing a finished
        # position: reset the row back to PENDING (clearing the stale result) so
        # the panel shows the "Analyzing…" state and the poller reveals the fresh
        # analysis. An already-PENDING row is left alone so the in-flight task is
        # not duplicated.
        if created or row.status != CoachSuggestion.Status.PENDING:
            if not created:
                row.status = CoachSuggestion.Status.PENDING
                row.eval_text = ""
                row.eval_cp = None
                row.best_move_san = None
                row.best_move_uci = None
                row.analysis = ""
                await row.asave()
            await sync_to_async(analyze_game_task.delay)(
                user.id, id, fen, game.get("pgn")
            )

    # Re-render the body from stored state. The existing HTMX poller reveals the
    # result once the worker finishes; the request no longer blocks on analysis.
    history = [s async for s in CoachSuggestion.objects.filter(user=user, game_id=id)]
    stored, highlight = _suggestion_for_fen(history, fen)
    context = _build_detail_context(
        game_data,
        user.chess_username,
        id,
        highlight=highlight,
        analysis=stored["analysis"] if stored else None,
        suggestion=stored,
        history=history,
        can_analyze=True,
    )
    return render(request, "partials/game_body.html", context)


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
