from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .services.chess_client import Client
from .services.coach import get_best_move


def _client_for(user) -> Client:
    """Build a Chess.com client for the authenticated user."""
    return Client(username=user.chess_username)


@login_required
def home(request):
    """Home page: lists the user's current Chess.com games."""
    try:
        games = _client_for(request.user).my_current_games()
    except Exception as exc:
        return render(request, "error.html", {"message": str(exc)}, status=500)

    return render(request, "home.html", {"games": games})


@login_required
def game_list(request):
    """HTMX endpoint: returns the current-games fragment for polling."""
    try:
        games = _client_for(request.user).my_current_games()
    except Exception:
        games = []  # degrada silenziosamente: il polling non deve rompere la pagina
    return render(request, "partials/game_list.html", {"games": games})


@login_required
def game_detail(request, id):
    """Game detail page: renders the board and the AI-coach panel."""
    try:
        game_data = _client_for(request.user).game_detail(id)
    except Exception as exc:
        return render(request, "error.html", {"message": str(exc)}, status=500)

    if not game_data:
        return render(
            request,
            "game.html",
            {"id": id, "error": "Game not found or no longer active."},
        )

    game = game_data["game"]
    white_name = game_data["white_name"]
    username = request.user.chess_username
    # DTL has no inline ternary, so orientation is decided here.
    orientation = "white" if white_name.lower() == username.lower() else "black"

    return render(
        request,
        "game.html",
        {
            "id": id,
            "game": game,
            "white_name": white_name,
            "black_name": game_data["black_name"],
            "fen": game.get("fen"),
            "pgn": game.get("pgn"),
            "orientation": orientation,
        },
    )


@login_required
async def coach_suggestion(request, id):
    """HTMX endpoint: returns the AI coach's analysis fragment for a game."""
    # Async view: resolve the user via auser() to avoid a synchronous DB
    # access (request.user is lazy and would raise SynchronousOnlyOperation).
    user = await request.auser()
    game_data = _client_for(user).game_detail(id)
    if not game_data:
        return render(
            request,
            "partials/coach_suggestion.html",
            {"analysis": "Game not found or no longer active."},
        )

    game = game_data["game"]
    analysis = await get_best_move(game.get("fen", ""), game.get("pgn"))
    return render(request, "partials/coach_suggestion.html", {"analysis": analysis})


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
