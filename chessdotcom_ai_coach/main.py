import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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
        games = games_data.get("games", []) if isinstance(games_data, dict) else []

        return templates.TemplateResponse("game_list.html", {"request": request, "games": games})
    except Exception as e:
        # In case of error, return a simple message (could be a dedicated template)
        return HTMLResponse(
            content=f'<div class="text-red-500 p-4 bg-red-900/20 rounded">Error: {str(e)}</div>', status_code=500
        )
