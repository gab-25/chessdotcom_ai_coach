import os
import tomllib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from dotenv import load_dotenv

from chessdotcom_ai_coach.routers import pages
from chessdotcom_ai_coach.dependencies import create_db_and_tables, BASE_DIR, get_client, get_session

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan)

# Template Configuration
app.state.templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Static Files Configuration
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Load version from pyproject.toml
try:
    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
        app.state.version = data.get("project", {}).get("version")
except FileNotFoundError:
    app.state.version = "0.1.0"

# Include Routers
app.include_router(pages.router, dependencies=[get_session, get_client])
