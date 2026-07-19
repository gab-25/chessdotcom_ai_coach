from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import CoachSuggestion
from .services import board as board_utils
from .services import game_store
from .services.chess_client import Client
from .services.coach import get_best_move


def _client_for(user) -> Client:
    """Build a Chess.com client for the authenticated user."""
    return Client(username=user.chess_username)


def _decorate_games(games):
    """Attach the glyph-board cells and move number used by the game cards."""
    for game in games:
        fen = game.get("fen")
        game["cells"] = board_utils.fen_to_cells(fen)
        game["move_no"] = board_utils.fullmove_number(fen)
    return games


def _decorate_past_games(games):
    """Attach board cells + move number to persisted Game rows (history cards)."""
    for game in games:
        game.cells = board_utils.fen_to_cells(game.fen)
        game.move_no = board_utils.fullmove_number(game.fen)
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
        "turn_label": board_utils.active_color(fen).capitalize(),
        "last_move": board_utils.last_move_from_pgn(pgn),
        "analysis": analysis,
        "eval_fill": _eval_fill(suggestion["eval_cp"]) if suggestion else 50,
        "best_move_san": suggestion["best_move_san"] if suggestion else None,
        "eval_text": suggestion["eval_text"] if suggestion else None,
        "moves": moves,
        "history": history,
        "can_analyze": can_analyze,
    }


def _fetch_detail(user, id):
    """Fetch a game and return ``(game_data, error_message)``."""
    game_data = _client_for(user).game_detail(id)
    if not game_data:
        return None, "Game not found or no longer active."
    return game_data, None


@login_required
def home(request):
    """Home page: lists the user's current games plus the past-games history."""
    try:
        games = _decorate_games(_client_for(request.user).my_current_games())
        game_store.upsert_current_games(request.user, games)
    except Exception as exc:
        return render(request, "error.html", {"message": str(exc)}, status=500)

    past = _decorate_past_games(game_store.past_games(request.user))
    return render(request, "home.html", {"games": games, "past_games": past})


@login_required
def game_list(request):
    """HTMX endpoint: current games + past-games history fragment for polling.

    Snapshots the current games on every poll so the history stays fresh even
    without opening a game.
    """
    try:
        games = _decorate_games(_client_for(request.user).my_current_games())
        game_store.upsert_current_games(request.user, games)
    except Exception:
        games = []  # degrada silenziosamente: il polling non deve rompere la pagina
    past = _decorate_past_games(game_store.past_games(request.user))
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
        return render(request, "error.html", {"message": str(exc)}, status=500)

    can_analyze = True
    if error:
        stored = game_store.stored_game(request.user, id)
        if stored is None:
            return render(request, "game.html", {"id": id, "error": error})
        game_data = _game_data_from_stored(stored)
        can_analyze = False  # past game: history is read-only

    history = list(CoachSuggestion.objects.filter(user=request.user, game_id=id))
    context = _build_detail_context(
        game_data,
        request.user.chess_username,
        id,
        history=history,
        can_analyze=can_analyze,
    )
    return render(request, "game.html", context)


@login_required
async def coach_suggestion(request, id):
    """HTMX endpoint: re-renders the game body with the coach's analysis.

    Swapped into ``#game-body`` so the recommended move lights up on the board
    and the eval bar fills in a single response.
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
    suggestion = await get_best_move(fen, game.get("pgn"))
    highlight = _uci_to_squares(suggestion["best_move_uci"])

    # One analysis per position: re-analysing the same FEN overwrites the row.
    await CoachSuggestion.objects.aupdate_or_create(
        user=user,
        game_id=id,
        fen=fen,
        defaults={
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": suggestion["eval_text"],
            "eval_cp": suggestion["eval_cp"],
            "best_move_san": suggestion["best_move_san"],
            "best_move_uci": suggestion["best_move_uci"],
            "analysis": suggestion["analysis"],
        },
    )
    history = [
        s
        async for s in CoachSuggestion.objects.filter(user=user, game_id=id)
    ]

    context = _build_detail_context(
        game_data,
        user.chess_username,
        id,
        highlight=highlight,
        analysis=suggestion["analysis"],
        suggestion=suggestion,
        history=history,
        can_analyze=True,
    )
    return render(request, "partials/game_body.html", context)


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
