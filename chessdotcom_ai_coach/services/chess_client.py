import re
from typing import Dict, List, Optional

from chessdotcom import ChessDotComClient
from django.utils import timezone

# Chess.com per-side `result` strings that mean the game was drawn. Anything that
# isn't one of these and isn't "win" is treated as a loss for that side.
_DRAW_RESULTS = {
    "stalemate",
    "agreed",
    "repetition",
    "insufficient",
    "50move",
    "timevsinsufficient",
}

# Map Chess.com's raw per-side `result` codes to a short, human-friendly reason.
_RESULT_DETAIL = {
    "win": "",
    "checkmated": "checkmate",
    "resigned": "resignation",
    "timeout": "timeout",
    "abandoned": "abandonment",
    "stalemate": "stalemate",
    "agreed": "agreement",
    "repetition": "repetition",
    "insufficient": "insufficient material",
    "50move": "50-move rule",
    "timevsinsufficient": "timeout vs insufficient",
}


class Client:
    """
    Client for interacting with the Chess.com API.
    """

    def __init__(self, username: str) -> None:
        self._chessdotcomclient = ChessDotComClient(
            user_agent="Chessdotcom-AI-Coach (Contact: gabrielesorci.25@gmail.com)"
        )
        self.username = username

    def my_current_games(self) -> List:
        """
        Returns the current games for the authenticated user.
        """
        response = self._chessdotcomclient.get_player_current_games(self.username)  # pyright: ignore[reportAttributeAccessIssue]
        # The chessdotcom library returns an object with a .json attribute (property or dictionary)
        games_data = response.json
        raw_games = games_data.get("games", []) if isinstance(games_data, dict) else []

        processed_games = []
        for game in raw_games:
            pgn = game.get("pgn", "")

            # Extract White and Black info from PGN
            white_match = re.search(r'\[White "(.*?)"\]', pgn)
            black_match = re.search(r'\[Black "(.*?)"\]', pgn)
            white_elo_match = re.search(r'\[WhiteElo "(.*?)"\]', pgn)
            black_elo_match = re.search(r'\[BlackElo "(.*?)"\]', pgn)

            # Fallback to URL if PGN parsing fails for username
            white_user = "Unknown"
            if white_match:
                white_user = white_match.group(1)
            elif "white" in game and isinstance(game["white"], str):
                white_user = game["white"].split("/")[-1]

            black_user = "Unknown"
            if black_match:
                black_user = black_match.group(1)
            elif "black" in game and isinstance(game["black"], str):
                black_user = game["black"].split("/")[-1]

            game["white"] = {
                "username": white_user,
                "rating": white_elo_match.group(1) if white_elo_match else "?",
            }
            game["black"] = {
                "username": black_user,
                "rating": black_elo_match.group(1) if black_elo_match else "?",
            }
            game["is_my_turn"] = game.get("turn", "").lower() == (
                "white" if white_user.lower() == self.username.lower() else "black"
            )

            # Extract game ID from URL
            # Example URL: https://www.chess.com/game/daily/944768131
            game_url = game.get("url", "")
            game_id = game_url.split("/")[-1] if game_url else ""
            game["game_id"] = game_id

            processed_games.append(game)

        return processed_games

    def game_detail(self, id: str) -> Dict | None:
        """
        Returns the game detail for a given game ID.
        """
        response = self._chessdotcomclient.get_player_current_games(self.username)  # pyright: ignore[reportAttributeAccessIssue]
        games_data = response.json
        games = games_data.get("games", []) if isinstance(games_data, dict) else []

        # Find the specific game by ID (last part of the URL)
        game_detail = next((g for g in games if g.get("url", "").split("/")[-1] == id), None)

        if not game_detail:
            return None

        # Process PGN for player names and ratings
        pgn = game_detail.get("pgn", "")
        white_match = re.search(r'\[White "(.*?)"\]', pgn)
        black_match = re.search(r'\[Black "(.*?)"\]', pgn)
        white_elo_match = re.search(r'\[WhiteElo "(.*?)"\]', pgn)
        black_elo_match = re.search(r'\[BlackElo "(.*?)"\]', pgn)

        white_name = white_match.group(1) if white_match else "White"
        black_name = black_match.group(1) if black_match else "Black"

        return {
            "game": game_detail,
            "white_name": white_name,
            "black_name": black_name,
            "white_rating": white_elo_match.group(1) if white_elo_match else None,
            "black_rating": black_elo_match.group(1) if black_elo_match else None,
        }

    def finished_game_results(
        self, year: Optional[int] = None, month: Optional[int] = None
    ) -> Dict[str, dict]:
        """Outcomes of the user's finished games for one month, from the archives.

        Chess.com drops a game from ``current_games`` the moment it ends, so the
        snapshot we hold has a PGN with Result "*". The monthly-archive endpoint is
        the reliable source for the final result: unlike current games (where
        ``white``/``black`` are URL strings), each archived game carries
        ``white``/``black`` as dicts with a ``result`` code.

        Returns ``{game_id: {"result": "win"|"loss"|"draw", "detail": str}}`` keyed
        by the Chess.com game id (last URL segment), covering only the games the
        user played. Defaults to the current month when ``year``/``month`` are None.
        """
        # The chessdotcom library requires both year and month (or a datetime);
        # passing None for either raises ValueError, so default to the current month.
        if year is None or month is None:
            now = timezone.now()
            year, month = now.year, now.month
        response = self._chessdotcomclient.get_player_games_by_month(  # pyright: ignore[reportAttributeAccessIssue]
            self.username, year, month
        )
        data = response.json
        games = data.get("games", []) if isinstance(data, dict) else []

        me = self.username.lower()
        results: Dict[str, dict] = {}
        for game in games:
            white = game.get("white") or {}
            black = game.get("black") or {}
            if not isinstance(white, dict) or not isinstance(black, dict):
                continue  # not the archive shape (defensive)

            if str(white.get("username", "")).lower() == me:
                mine, theirs = white, black
            elif str(black.get("username", "")).lower() == me:
                mine, theirs = black, white
            else:
                continue  # archive can include games under an alias we don't match

            my_result = str(mine.get("result", ""))
            if not my_result:
                continue

            if my_result == "win":
                outcome = "win"
                # The decisive reason lives on the losing side.
                detail = _RESULT_DETAIL.get(str(theirs.get("result", "")), "")
            elif my_result in _DRAW_RESULTS:
                outcome = "draw"
                detail = ""
            else:
                outcome = "loss"
                detail = _RESULT_DETAIL.get(my_result, "")

            game_url = game.get("url", "")
            game_id = game_url.split("/")[-1] if game_url else ""
            if game_id:
                results[game_id] = {"result": outcome, "detail": detail}

        return results
