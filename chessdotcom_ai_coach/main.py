import os
import tomllib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette import status
from dotenv import load_dotenv

from chessdotcom_ai_coach.routers import pages, auth
from chessdotcom_ai_coach.dependencies import create_db_and_tables, AuthRedirectException, auth_required

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan)

# Session Configuration
# In a real app, use a secure secret key from environment variables
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "a-very-secret-key"))


@app.exception_handler(AuthRedirectException)
async def auth_redirect_exception_handler(request, exc):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


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
app.include_router(auth.router)
app.include_router(pages.router, dependencies=[Depends(auth_required)])
