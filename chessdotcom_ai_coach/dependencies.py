import os
from typing import Annotated

from fastapi import Depends, Request, HTTPException, status
from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine, select

from chessdotcom_ai_coach.client import Client

load_dotenv()


class AuthRedirectException(Exception):
    pass


# Database Configuration
sqlite_file_name = os.getenv("DATABASE_NAME", "database.db")
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


def create_db_and_tables():
    from chessdotcom_ai_coach.user import User

    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        statement = select(User).where(User.username == "admin")
        existing_user = session.exec(statement).first()

        if not existing_user:
            default_user = User(
                username="admin",
                password="password123",
            )
            session.add(default_user)
            session.commit()
            print("--- Default user 'admin' has been created ---")


def get_session():
    with Session(engine) as session:
        yield session


def get_client(request: Request):
    chess_username = request.session.get("chess_username")
    if not chess_username:
        return None
    return Client(username=chess_username)


def auth_required(request: Request):
    """
    Checks if the user is authenticated (not a Guest).
    Raises AuthRedirectException if not.
    """
    username = request.session.get("username")
    if not username:
        raise AuthRedirectException()
    return username


SessionDep = Annotated[Session, Depends(get_session)]
ClientDep = Annotated[Client | None, Depends(get_client)]
