import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from chessdotcom_ai_coach.auth_service import ZITADEL_URL, get_user_metadata, oauth

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/auth/zitadel")
async def login_zitadel(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.zitadel.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    token = await oauth.zitadel.authorize_access_token(request)
    access_token = token.get("access_token")

    user_info = dict(await oauth.zitadel.userinfo(token=token))
    sub = user_info.get("sub")
    display_name = (
        user_info.get("name")
        or user_info.get("preferred_username")
        or user_info.get("email")
        or sub
    )

    async with httpx.AsyncClient() as client:
        chess_username = await get_user_metadata(client, access_token, "chessdotcom_username")

    request.session["sub"] = sub
    request.session["display_name"] = display_name
    request.session["access_token"] = access_token
    request.session["chess_username"] = chess_username or ""

    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    post_logout_uri = str(request.url_for("login_page"))
    return RedirectResponse(
        url=f"{ZITADEL_URL}/oidc/v1/end_session?post_logout_redirect_uri={post_logout_uri}",
        status_code=303,
    )
