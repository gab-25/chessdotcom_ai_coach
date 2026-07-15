from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .services import board as board_utils
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
    game_data, username, id, *, highlight=None, analysis=None, suggestion=None
):
    """Assemble the game-detail template context from already-fetched data.

    Pure (no IO) so the async coach view can fetch once and rebuild cheaply.
    """
    game = game_data["game"]
    fen = game.get("fen")
    white_name = game_data["white_name"]
    orientation = "white" if white_name.lower() == username.lower() else "black"

    return {
        "id": id,
        "game": game,
        "white_name": white_name,
        "black_name": game_data["black_name"],
        "white_rating": game_data.get("white_rating"),
        "black_rating": game_data.get("black_rating"),
        "fen": fen,
        "pgn": game.get("pgn"),
        "orientation": orientation,
        "cells": board_utils.fen_to_cells(
            fen, highlight=highlight, flipped=(orientation == "black")
        ),
        "move_no": board_utils.fullmove_number(fen),
        "turn_label": board_utils.active_color(fen).capitalize(),
        "last_move": board_utils.last_move_from_pgn(game.get("pgn")),
        "analysis": analysis,
        "eval_fill": _eval_fill(suggestion["eval_cp"]) if suggestion else 50,
        "best_move_san": suggestion["best_move_san"] if suggestion else None,
        "eval_text": suggestion["eval_text"] if suggestion else None,
    }


def _fetch_detail(user, id):
    """Fetch a game and return ``(game_data, error_message)``."""
    game_data = _client_for(user).game_detail(id)
    if not game_data:
        return None, "Game not found or no longer active."
    return game_data, None


@login_required
def home(request):
    """Home page: lists the user's current Chess.com games."""
    try:
        games = _decorate_games(_client_for(request.user).my_current_games())
    except Exception as exc:
        return render(request, "error.html", {"message": str(exc)}, status=500)

    return render(request, "home.html", {"games": games})


@login_required
def game_list(request):
    """HTMX endpoint: returns the current-games fragment for polling."""
    try:
        games = _decorate_games(_client_for(request.user).my_current_games())
    except Exception:
        games = []  # degrada silenziosamente: il polling non deve rompere la pagina
    return render(request, "partials/game_list.html", {"games": games})


@login_required
def game_detail(request, id):
    """Game detail page: renders the board and the AI-coach panel."""
    try:
        game_data, error = _fetch_detail(request.user, id)
    except Exception as exc:
        return render(request, "error.html", {"message": str(exc)}, status=500)

    if error:
        return render(request, "game.html", {"id": id, "error": error})

    context = _build_detail_context(game_data, request.user.chess_username, id)
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
        # DB, and base.html's header reads request.user.
        return render(request, "partials/detail_error.html", {"error": error})

    game = game_data["game"]
    suggestion = await get_best_move(game.get("fen", ""), game.get("pgn"))
    highlight = _uci_to_squares(suggestion["best_move_uci"])

    context = _build_detail_context(
        game_data,
        user.chess_username,
        id,
        highlight=highlight,
        analysis=suggestion["analysis"],
        suggestion=suggestion,
    )
    return render(request, "partials/game_body.html", context)


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
