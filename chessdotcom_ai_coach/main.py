import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import re
from fastapi.templating import Jinja2Templates
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


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """
    Serves the home page.
    """
    return templates.TemplateResponse("home_page.html", {"request": request, "username": USERNAME})


@app.get("/detail", response_class=HTMLResponse)
async def detail_page(request: Request):
    """
    Serves the detail page.
    """
    return templates.TemplateResponse("detail_page.html", {"request": request, "username": USERNAME})


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
            processed_games.append(game)

        return templates.TemplateResponse("game_list.html", {"request": request, "games": processed_games})
    except Exception as e:
        # In case of error, return a simple message (could be a dedicated template)
        return HTMLResponse(
            content=f'<div class="text-red-500 p-4 bg-red-900/20 rounded">Error: {str(e)}</div>', status_code=500
        )
