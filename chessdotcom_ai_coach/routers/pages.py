import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """
    Serves the home page.
    """
    try:
        templates = request.app.state.templates
        username = request.app.state.username
        response = request.app.state.client.get_player_current_games(username)
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
                "white" if white_user.lower() == username.lower() else "black"
            )

            # Extract game ID from URL
            # Example URL: https://www.chess.com/game/daily/944768131
            game_url = game.get("url", "")
            game_id = game_url.split("/")[-1] if game_url else ""
            game["game_id"] = game_id

            processed_games.append(game)

        return templates.TemplateResponse(
            "home_page.html",
            {
                "request": request,
                "username": request.app.state.username,
                "version": request.app.state.version,
                "games": processed_games,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving current games: {str(e)}", status_code=500)


@router.get("/game/{id}", response_class=HTMLResponse)
async def game_page(request: Request, id: str):
    """
    Serves the game page by id.
    """
    try:
        templates = request.app.state.templates
        username = request.app.state.username
        response = request.app.state.client.get_player_current_games(username)
        games_data = response.json
        games = games_data.get("games", []) if isinstance(games_data, dict) else []

        # Find the specific game by ID (last part of the URL)
        game_detail = next((g for g in games if g.get("url", "").split("/")[-1] == id), None)

        if not game_detail:
            return templates.TemplateResponse(
                "pages/game.html",
                {"request": request, "username": username, "id": id, "error": "Game not found or no longer active."},
            )

        # Process PGN for player names
        pgn = game_detail.get("pgn", "")
        white_match = re.search(r'\[White "(.*?)"\]', pgn)
        black_match = re.search(r'\[Black "(.*?)"\]', pgn)

        white_name = white_match.group(1) if white_match else "White"
        black_name = black_match.group(1) if black_match else "Black"

        return templates.TemplateResponse(
            "game_page.html",
            {
                "request": request,
                "username": username,
                "version": request.app.state.version,
                "id": id,
                "game": game_detail,
                "white_name": white_name,
                "black_name": black_name,
                "fen": game_detail.get("fen"),
                "pgn": pgn,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving game details: {str(e)}", status_code=500)
