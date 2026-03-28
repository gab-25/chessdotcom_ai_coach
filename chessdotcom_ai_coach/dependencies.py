import os
from typing import Annotated

from fastapi import Depends, Request, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine, select

from chessdotcom_ai_coach.client import Client
from chessdotcom_ai_coach.auth_service import SECRET_KEY, ALGORITHM, get_password_hash
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

# OAuth2 scheme for token-based authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)


def get_session():
    """
    Dependency to provide a SQLModel session.
    """
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


async def get_current_user(
    request: Request, session: SessionDep, token: Annotated[str | None, Depends(oauth2_scheme)] = None
) -> User | None:
    """
    Retrieves the current user from either a JWT token (OAuth2) or a Session (Web).
    Returns None if no user is found or authenticated.
    """
    username = None

    # 1. Try to get username from JWT Token (OAuth2 flow)
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])  # pyright: ignore[reportArgumentType]
            username = payload.get("sub")
        except JWTError:
            pass

    # 2. Try to get username from Session (Web interface flow)
    if not username:
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
