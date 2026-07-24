from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse

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
    """Rebuild the ``game_data`` shape the detail view expects from a Game row.

    Lets the detail view fall back to the persisted snapshot when a game is no
    longer current on Chess.com — this is also the finished-game (review) path.
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
        "result": stored.result,
        "result_label": stored.result_label,
        "result_detail": stored.result_detail,
    }


def _uci_to_squares(uci):
    """Turn a UCI move like ``"d4f5"`` into its from/to square names."""
    if uci and len(uci) >= 4:
        return [uci[0:2], uci[2:4]]
    return []


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


def _fetch_detail(user, id):
    """Fetch a game and return ``(game_data, error_message)``."""
    game_data = _client_for(user).game_detail(id)
    if not game_data:
        return None, "Game not found or no longer active."
    return game_data, None


def _suggestion_payload(row, *, followed=None, played_eval=None):
    """Shape a CoachSuggestion row for the client (an ``analysis`` map entry).

    ``pending`` while the analysis is in flight; otherwise the coach's move + eval
    + prose + arrow squares. ``followed``/``played_eval`` are set for *played*
    moves (review of a move the user made) and left off for the live to-move
    position, which has no played move to compare against.
    """
    if row is None:
        return None
    if row.status == CoachSuggestion.Status.PENDING:
        return {"pending": True}
    rec = _uci_to_squares(row.best_move_uci)
    payload = {
        "recSan": row.best_move_san or "",
        "recEval": row.eval_text or "",
        "fill": _eval_fill(row.eval_cp),
        "prose": row.analysis or "",
        "recFrom": rec[0] if rec else "",
        "recTo": rec[1] if rec else "",
    }
    if followed is not None:
        payload["followed"] = followed
        payload["playedEval"] = played_eval if followed else ""
    return payload


def _build_review_data(game_data, user, id, request, *, is_live):
    """Serialise a game into the JSON model the detail page steps through.

    Works for both a live game (fetched from Chess.com) and a finished one
    (rebuilt from the stored snapshot). The page renders any ply client-side from
    this data — the played moves (with from/to squares and the position the coach
    analyses), the FEN after each ply, and the coach analysis keyed by ply index.

    For a live game it also carries the *current* position: which side is to move,
    whether it is the user's turn, and any coach analysis for it — so the head of
    the timeline shows the live "your move to play" coach, and the client can poll
    for new moves.
    """
    game = game_data["game"]
    pgn = game.get("pgn")
    fen = game.get("fen")
    username = user.chess_username
    white_name = game_data["white_name"]
    orientation = "white" if white_name.lower() == username.lower() else "black"

    moves = board_utils.moves_from_pgn(pgn)
    positions = board_utils.positions_from_pgn(pgn)
    history = list(CoachSuggestion.objects.filter(user=user, game_id=id))
    board_utils.annotate_moves(moves, history)
    by_fen = {row.fen: row for row in history}

    plies = []
    analysis = {}
    for i, m in enumerate(moves, start=1):
        squares = _uci_to_squares(m["uci"])
        plies.append(
            {
                "no": m["move_no"],
                "color": m["color"],
                "san": m["san"],
                "from": squares[0] if squares else "",
                "to": squares[1] if squares else "",
                "fenBefore": m["fen_before"],
            }
        )
        suggestion = m["suggestion"]
        if suggestion is None:
            continue
        payload = _suggestion_payload(
            suggestion, followed=m["followed"], played_eval=suggestion.eval_text or ""
        )
        if payload is not None:
            analysis[str(i)] = payload

    live_head = len(plies)
    user_to_move = _is_user_turn(game_data, username) if fen else False
    head_analysis = _suggestion_payload(by_fen.get(fen)) if (is_live and fen) else None

    return {
        "plies": plies,
        "positions": positions,
        "analysis": analysis,
        "meta": {
            "orientation": orientation,
            "isLive": bool(is_live),
            "canAnalyze": True,
            "liveHead": live_head,
            "userToMove": bool(user_to_move),
            "headFen": fen or "",
            "headAnalysis": head_analysis,
        },
        "csrf": get_token(request),
        "urls": {
            "analyze": reverse("analyze_position", args=[id]),
            "poll": reverse("game_detail", args=[id]) + "?poll=1",
        },
    }


def _resolve_game(user, id):
    """Return ``(game_data, is_live, stored)`` for the detail page.

    Prefers the live game from Chess.com; falls back to the persisted snapshot
    (a finished game). ``is_live`` is True only when the game is still current.
    ``game_data`` is ``None`` when the game is neither current nor stored.
    """
    game_data, error = _fetch_detail(user, id)
    if not error:
        return game_data, True, game_store.stored_game(user, id)
    stored = game_store.stored_game(user, id)
    if stored is None:
        return None, False, None
    return _game_data_from_stored(stored), False, stored


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
    """The one detail page — a move-by-move walk over a game, live or finished.

    A live game is fetched from Chess.com (falling back to the stored snapshot
    once it ends); a finished game is served from the snapshot. Either way the
    page renders the same board + coach + history + moves UI and steps through the
    moves client-side. ``?poll=1`` returns the fresh JSON model instead of HTML so
    the live page can pick up new moves without a disruptive full-body swap.
    """
    poll = request.GET.get("poll") == "1"
    try:
        game_data, is_live, _ = _resolve_game(request.user, id)
    except Exception as exc:
        if poll:
            # Transient fetch failure mid-poll: skip this tick, keep polling.
            return HttpResponse(status=204)
        return render(request, "error.html", {"message": str(exc)}, status=500)

    if game_data is None:
        if poll:
            return HttpResponse(status=204)
        return render(request, "error.html", {"message": "Game not found."}, status=404)

    if not game_data["game"].get("pgn") and not game_data["game"].get("fen"):
        if poll:
            return HttpResponse(status=204)
        return render(
            request,
            "error.html",
            {"message": "This game can't be shown yet — its moves aren't available."},
        )

    review_data = _build_review_data(
        game_data, request.user, id, request, is_live=is_live
    )
    if poll:
        return JsonResponse(review_data)

    orientation = review_data["meta"]["orientation"]
    return render(
        request,
        "game_detail.html",
        {
            "id": id,
            "is_live": is_live,
            "white_name": game_data["white_name"],
            "black_name": game_data["black_name"],
            "white_rating": game_data.get("white_rating"),
            "black_rating": game_data.get("black_rating"),
            "result": game_data.get("result"),
            "result_label": game_data.get("result_label"),
            "result_detail": game_data.get("result_detail"),
            "time_class": game_data["game"].get("time_class"),
            "orientation": orientation,
            "has_moves": bool(review_data["plies"]) or bool(game_data["game"].get("fen")),
            "review_data": review_data,
        },
    )


@login_required
def analyze_position(request, id):
    """On-demand coach analysis of a single position (any move, or the live turn).

    ``GET  ?fen=…`` returns the current status (the client polls this); ``POST``
    with ``fen`` enqueues background analysis (idempotent per position) reusing the
    same Celery task as before. Returns JSON either way.
    """
    fen = request.POST.get("fen") or request.GET.get("fen")
    if not fen:
        return JsonResponse({"error": "missing fen"}, status=400)

    row = CoachSuggestion.objects.filter(
        user=request.user, game_id=id, fen=fen
    ).first()

    if request.method == "POST":
        pgn = None
        try:
            game_data, error = _fetch_detail(request.user, id)
            if not error:
                pgn = game_data["game"].get("pgn")
        except Exception:
            game_data = None
        if pgn is None:
            stored = game_store.stored_game(request.user, id)
            pgn = stored.pgn if stored else None

        if row is None:
            row = CoachSuggestion.objects.create(
                user=request.user,
                game_id=id,
                fen=fen,
                status=CoachSuggestion.Status.PENDING,
                move_no=board_utils.fullmove_number(fen),
                eval_text="",
                analysis="",
            )
            analyze_game_task.delay(request.user.id, id, fen, pgn)
        elif row.status != CoachSuggestion.Status.PENDING:
            # Re-analyse: reset to PENDING (clear the stale result) and re-enqueue.
            # An already-PENDING row is left alone so the task is not duplicated.
            row.status = CoachSuggestion.Status.PENDING
            row.eval_text = ""
            row.eval_cp = None
            row.best_move_san = None
            row.best_move_uci = None
            row.analysis = ""
            row.save()
            analyze_game_task.delay(request.user.id, id, fen, pgn)

    payload = _suggestion_payload(row)
    if payload is None:
        return JsonResponse({"status": "none"})
    if payload.get("pending"):
        return JsonResponse({"status": "pending"})
    payload["status"] = "done"
    return JsonResponse(payload)


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
