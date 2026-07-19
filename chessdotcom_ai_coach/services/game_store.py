"""Persistence for the game history.

Chess.com only serves games that are still "current", so the home polling is our
one chance to snapshot them. These helpers upsert the fetched games into the
``Game`` table and expose the past ones for the history views. Kept separate from
the Chess.com ``Client`` (which does pure IO) and from the views (which stay thin).
"""

from __future__ import annotations

from typing import List

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


def past_games(user) -> List[Game]:
    """Games that are no longer current, newest first (for the home history)."""
    return list(Game.objects.filter(user=user, is_active=False))


def stored_game(user, game_id: str) -> Game | None:
    """The persisted snapshot for a game id, or ``None`` (for game_detail fallback)."""
    return Game.objects.filter(user=user, game_id=game_id).first()
