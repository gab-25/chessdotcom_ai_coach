import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import tomllib
from dotenv import load_dotenv
from chessdotcom import ChessDotComClient

from chessdotcom_ai_coach.routers import pages, components

load_dotenv()

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Template Configuration
app.state.templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Static Files Configuration
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Chess.com Client Configuration
USER_AGENT = "Chessdotcom-AI-Coach (Contact: gabrielesorci.25@gmail.com)"
USERNAME = os.getenv("CHESSDOTCOM_USERNAME")
app.state.client = ChessDotComClient(user_agent=USER_AGENT)
app.state.username = USERNAME

# Load version from pyproject.toml
with open("pyproject.toml", "rb") as f:
    data = tomllib.load(f)
    app.state.version = data.get("project", {}).get("version")

# Include Routers
app.include_router(pages.router)
app.include_router(components.router)
