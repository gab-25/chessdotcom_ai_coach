from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import select

from chessdotcom_ai_coach.dependencies import SessionDep
from chessdotcom_ai_coach.user import User
from chessdotcom_ai_coach.auth_service import (
    verify_password,
    create_access_token,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

router = APIRouter()


@router.post("/token")
async def login_for_access_token(
    session: SessionDep,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    """
    OAuth2 compatible token login, retrieve an access token for future requests.
    """
    statement = select(User).where(User.username == form_data.username)
    user = session.exec(statement).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """
    Serves the login page.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "username": "Guest",
            "version": request.app.state.version,
        },
    )


@router.post("/login")
async def login(
    request: Request,
    session: SessionDep,
    username: str = Form(...),
    password: str = Form(...),
):
    """
    Handles login logic by checking credentials against the database.
    """
    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()

    if not user or not verify_password(password, user.hashed_password):
        templates = request.app.state.templates
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "username": "Guest",
                "version": getattr(request.app.state, "version", "0.1.0"),
                "error": "Invalid username or password",
            },
        )

    # Use session to store the username for the current user (Web flow)
    request.session["username"] = user.username
    # Store the chess.com username in the session for the client
    request.session["chess_username"] = user.chessdotcom_username or user.username

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
async def logout(request: Request):
    """
    Clears the current session.
    """
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
