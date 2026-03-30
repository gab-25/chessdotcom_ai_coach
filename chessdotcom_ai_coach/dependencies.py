import os
from typing import Annotated

from fastapi import Depends, Request
from dotenv import load_dotenv
from sqlmodel import Session, create_engine, select

from chessdotcom_ai_coach.client import Client
from chessdotcom_ai_coach.user import User

load_dotenv()


class AuthRedirectException(Exception):
    pass


# Database Configuration
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_HOST = os.getenv("POSTGRES_HOST")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")

# Constructing the PostgreSQL connection URL
DATABASE_URL = os.getenv(
    "DATABASE_URL", f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# Create the engine (PostgreSQL doesn't need check_same_thread=False)
engine = create_engine(DATABASE_URL)


def get_session():
    """
    Dependency to provide a SQLModel session.
    """
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


async def get_current_user(request: Request, session: SessionDep) -> User | None:
    """
    Retrieves the current user from the Session (Web interface flow).
    Returns None if no user is found or authenticated.
    """
    username = request.session.get("username")

    if not username:
        return None

    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()
    return user


def auth_required(user: Annotated[User | None, Depends(get_current_user)]):
    """
    Dependency that requires the user to be authenticated.
    Raises AuthRedirectException to trigger a redirect to /login.
    """
    if not user:
        raise AuthRedirectException()
    return user


def get_client(user: Annotated[User | None, Depends(get_current_user)]):
    """
    Dependency to provide a Chess.com client instance for the authenticated user.
    """
    if not user:
        return None
    # Use chessdotcom_username if set, otherwise fallback to the app username
    chess_username = user.chessdotcom_username or user.username
    return Client(username=chess_username)


ClientDep = Annotated[Client | None, Depends(get_client)]
