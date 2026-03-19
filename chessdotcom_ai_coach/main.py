import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import re
from fastapi.templating import Jinja2Templates
import tomllib
from dotenv import load_dotenv
from chessdotcom import ChessDotComClient

load_dotenv()

app = FastAPI()

# Template Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Static Files Configuration
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Chess.com Client Configuration
USER_AGENT = os.getenv("USER_AGENT", "Chess-AI-Coach-App/1.0 (contact: your-email@example.com)")
client = ChessDotComClient(user_agent=USER_AGENT)
USERNAME = os.getenv("CHESSDOTCOM_USERNAME")


# Load version from pyproject.toml
def get_version():
    try:
        pyproject_path = os.path.join(os.path.dirname(BASE_DIR), "pyproject.toml")
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            return data.get("project", {}).get("version", "0.1.0")
    except Exception:
        return "0.1.0"


VERSION = get_version()


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """
    Serves the home page.
    """
    return templates.TemplateResponse("home_page.html", {"request": request, "username": USERNAME, "version": VERSION})


@app.get("/detail-board/{id}", response_class=HTMLResponse)
async def detail_board_page(request: Request, id: str):
    """
    Serves the detail board page by id.
    """
    try:
        response = client.get_player_current_games(USERNAME)
        games_data = response.json
        games = games_data.get("games", []) if isinstance(games_data, dict) else []

        # Find the specific game by ID (last part of the URL)
        game_detail = next((g for g in games if g.get("url", "").split("/")[-1] == id), None)

        if not game_detail:
            return templates.TemplateResponse(
                "detail_board_page.html",
                {"request": request, "username": USERNAME, "error": "Game not found or no longer active."},
            )

        # Process PGN for player names
        pgn = game_detail.get("pgn", "")
        white_match = re.search(r'\[White "(.*?)"\]', pgn)
        black_match = re.search(r'\[Black "(.*?)"\]', pgn)

        white_name = white_match.group(1) if white_match else "White"
        black_name = black_match.group(1) if black_match else "Black"

        return templates.TemplateResponse(
            "detail_board_page.html",
            {
                "request": request,
                "username": USERNAME,
                "version": VERSION,
                "game": game_detail,
                "white_name": white_name,
                "black_name": black_name,
                "fen": game_detail.get("fen"),
                "pgn": pgn,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving game details: {str(e)}", status_code=500)


@app.get("/current-games", response_class=HTMLResponse)
async def current_games(request: Request):
    """
    Retrieves ongoing games and returns the HTML fragment for HTMX.
    """
    try:
        response = client.get_player_current_games(USERNAME)
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
                "white" if white_user.lower() == USERNAME.lower() else "black"
            )

            # Extract game ID from URL
            # Example URL: https://www.chess.com/game/daily/944768131
            game_url = game.get("url", "")
            game_id = game_url.split("/")[-1] if game_url else ""
            game["game_id"] = game_id

            processed_games.append(game)

        return templates.TemplateResponse("game_list.html", {"request": request, "games": processed_games})
    except Exception as e:
        # In case of error, return a simple message (could be a dedicated template)
        return HTMLResponse(
            content=f'<div class="text-red-500 p-4 bg-red-900/20 rounded">Error: {str(e)}</div>', status_code=500
        )
