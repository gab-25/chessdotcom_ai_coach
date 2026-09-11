"""Persistence for the game history.

Games arrive from two directions and each has its own writer. The monthly
archives bring in every finished game, live and daily, through
``upsert_finished_games``; the *current games* endpoint brings in daily games
still being played through ``upsert_current_games``, whose real job is to notice
when one of them ends. ``past_games`` is what the app actually shows.

Kept separate from the Chess.com ``Client`` (which does pure IO) and from the
views (which stay thin).
"""

from __future__ import annotations

from typing import List

from django.db.models import QuerySet

from ..models import Game


def _player(game: dict, color: str) -> dict:
    """Return the ``{username, rating}`` sub-dict the Client attaches per side."""
    value = game.get(color)
    return value if isinstance(value, dict) else {}


def upsert_current_games(user, games: List[dict]) -> None:
    """Snapshot the user's current games and retire the ones that vanished.

    Each game (as shaped by ``Client.my_current_games``) is written to a ``Game``
    row keyed by ``(user, game_id)``. Games no longer in the current set are marked
    ``is_active=False`` so they move to the "past games" history.
    """
    seen: List[str] = []
    for game in games:
        game_id = game.get("game_id")
        if not game_id:
            continue
        white = _player(game, "white")
        black = _player(game, "black")
        Game.objects.update_or_create(
            user=user,
            game_id=game_id,
            defaults={
                "url": game.get("url", ""),
                "white_name": white.get("username", ""),
                "black_name": black.get("username", ""),
                "white_rating": str(white.get("rating", "")),
                "black_rating": str(black.get("rating", "")),
                "time_class": game.get("time_class", ""),
                "pgn": game.get("pgn", ""),
                "fen": game.get("fen", ""),
                "is_active": True,
            },
        )
        seen.append(game_id)

    # Everything we didn't just see is no longer a current game.
    Game.objects.filter(user=user, is_active=True).exclude(game_id__in=seen).update(
        is_active=False
    )


def past_games(user, time_class: str = "") -> QuerySet[Game]:
    """Finished games for the user, newest first — the only list the app shows.

    Returns a **queryset, not a list**: an imported archive runs to thousands of
    rows, so the caller pages and counts in the database rather than in Python.
    ``time_class`` narrows to one Chess.com time control ("bullet", "blitz",
    "rapid", "daily"); empty means all of them.
    """
    games = Game.objects.filter(user=user, is_active=False)
    if time_class:
        games = games.filter(time_class=time_class)
    return games


def time_classes(user) -> List[str]:
    """The time controls this user actually has games for, for the home filter.

    Read from the data rather than hard-coded, so the filter never offers a
    choice that would come back empty.
    """
    return sorted(
        tc
        for tc in Game.objects.filter(user=user, is_active=False)
        .values_list("time_class", flat=True)
        .distinct()
        if tc
    )


def upsert_finished_games(user, games: List[dict]) -> int:
    """Store finished games from the monthly archive. Returns how many were new.

    Each entry is one of ``Client.finished_games``' normalised dicts. Two
    invariants matter here:

    * **``is_active`` is always False.** An archived game is finished by
      definition, so this must never resurrect a row that
      ``upsert_current_games`` retired — that flag is what decides whether a game
      is shown at all.
    * **an empty PGN never overwrites a stored one.** The archive is normally the
      better copy (ours stops at the last sync before the game left "current
      games"), but a blank one carries no moves and would destroy the only record
      we have.
    """
    created_count = 0
    for game in games:
        game_id = game.get("game_id")
        if not game_id:
            continue
        white = game.get("white") or {}
        black = game.get("black") or {}
        fields = {
            "url": game.get("url", ""),
            "white_name": white.get("username", ""),
            "black_name": black.get("username", ""),
            "white_rating": str(white.get("rating", "")),
            "black_rating": str(black.get("rating", "")),
            "time_class": game.get("time_class", ""),
            "fen": game.get("fen", ""),
            "end_time": game.get("end_time"),
            "result": game.get("result", Game.Result.UNKNOWN),
            "result_detail": game.get("result_detail", ""),
            "is_active": False,
        }
        pgn = game.get("pgn", "")
        if pgn:
            fields["pgn"] = pgn
        _row, created = Game.objects.update_or_create(
            user=user, game_id=game_id, defaults=fields
        )
        if created:
            created_count += 1
    return created_count


def stored_game(user, game_id: str) -> Game | None:
    """The stored game for an id, or ``None`` when we never saw it."""
    return Game.objects.filter(user=user, game_id=game_id).first()
