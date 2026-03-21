from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select
from chessdotcom_ai_coach.dependencies import SessionDep
from chessdotcom_ai_coach.user import User

router = APIRouter()


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
            "version": getattr(request.app.state, "version", "0.1.0"),
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

    if not user or user.password != password:
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

    # Use session to store the username for the current user
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
