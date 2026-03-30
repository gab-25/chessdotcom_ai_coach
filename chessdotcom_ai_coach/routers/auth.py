from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlmodel import select

from chessdotcom_ai_coach.dependencies import SessionDep
from chessdotcom_ai_coach.user import User
from chessdotcom_ai_coach.auth_service import oauth

router = APIRouter()


@router.get("/login")
async def login(request: Request):
    """
    Redirects to Zitadel for authentication.
    """
    redirect_uri = request.url_for("auth_callback")
    return await oauth.zitadel.authorize_redirect(request, str(redirect_uri))


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, session: SessionDep):
    """
    Handles the callback from Zitadel and initializes the user session.
    """
    token = await oauth.zitadel.authorize_access_token(request)
    user_info = token.get("userinfo")
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Failed to retrieve user information from Zitadel"
        )

    # Zitadel typically provides 'preferred_username' or 'email'
    username = user_info.get("preferred_username") or user_info.get("email") or user_info.get("sub")

    # Check if user exists in local DB to maintain chessdotcom settings
    statement = select(User).where(User.username == username)
    user = session.exec(statement).first()

    if not user:
        # Create a user for Zitadel authenticated users
        user = User(username=username)
        session.add(user)
        session.commit()
        session.refresh(user)

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
