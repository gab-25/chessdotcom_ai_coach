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

    # In a production app, we would use sessions or JWT.
    # For now, we set the username in the app state as requested by the existing architecture.
    request.app.state.username = user.username
    # If the user has a chessdotcom_username, we should use that for the client
    chess_user = user.chessdotcom_username or user.username

    # Re-initialize the client with the correct username
    from chessdotcom_ai_coach.client import Client

    request.app.state.client = Client(username=chess_user)

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
async def logout(request: Request):
    """
    Clears the current session.
    """
    request.app.state.username = "Guest"
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
