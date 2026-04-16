import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from chessdotcom_ai_coach.auth_service import set_user_metadata, update_display_name
from chessdotcom_ai_coach.dependencies import ClientDep, auth_required
from chessdotcom_ai_coach.game_service import get_best_move

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: dict = Depends(auth_required),
    client: ClientDep = None,
):
    if client is None:
        return RedirectResponse(url="/profile", status_code=303)
    try:
        templates = request.app.state.templates
        games = client.my_current_games()
        return templates.TemplateResponse(
            "home_page.html",
            {
                "request": request,
                "username": user["display_name"],
                "version": getattr(request.app.state, "version", "0.1.0"),
                "games": games,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving current games: {str(e)}", status_code=500)


@router.get("/game/{id}", response_class=HTMLResponse)
async def game_page(
    request: Request,
    id: str,
    user: dict = Depends(auth_required),
    client: ClientDep = None,
):
    try:
        templates = request.app.state.templates

        if not client:
            return HTMLResponse(content="Client not initialized", status_code=500)

        game_data = client.game_detail(id)

        if not game_data:
            return templates.TemplateResponse(
                "game_page.html",
                {
                    "request": request,
                    "username": user["display_name"],
                    "id": id,
                    "error": "Game not found or no longer active.",
                },
            )

        return templates.TemplateResponse(
            "game_page.html",
            {
                "request": request,
                "username": user["display_name"],
                "version": getattr(request.app.state, "version", "0.1.0"),
                "id": id,
                "game": game_data["game"],
                "white_name": game_data["white_name"],
                "black_name": game_data["black_name"],
                "fen": game_data["game"].get("fen"),
                "pgn": game_data["game"].get("pgn"),
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving game details: {str(e)}", status_code=500)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: dict = Depends(auth_required),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "username": user["display_name"],
            "display_name": user["display_name"],
            "chessdotcom_username": request.session.get("chess_username", ""),
        },
    )


@router.post("/profile", response_class=HTMLResponse)
async def profile_update(
    request: Request,
    user: dict = Depends(auth_required),
    display_name: str = Form(...),
    chessdotcom_username: str = Form(...),
):
    templates = request.app.state.templates
    access_token = user["access_token"]
    error = None

    async with httpx.AsyncClient() as client:
        try:
            await update_display_name(client, access_token, display_name)
            await set_user_metadata(client, access_token, "chessdotcom_username", chessdotcom_username)
        except Exception as e:
            error = str(e)

    if not error:
        request.session["display_name"] = display_name
        request.session["chess_username"] = chessdotcom_username

    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "username": request.session.get("display_name", user["display_name"]),
            "display_name": request.session.get("display_name", user["display_name"]),
            "chessdotcom_username": request.session.get("chess_username", ""),
            "success": not error,
            "error": error,
        },
    )


@router.post("/game/{id}/suggest", response_class=HTMLResponse)
async def get_suggestion(
    request: Request,
    id: str,
    fen: str = Form(...),
    pgn: str = Form(None),
    user: dict = Depends(auth_required),
):
    try:
        suggestion = await get_best_move(fen, pgn)
        return HTMLResponse(
            content=f"""
            <div class="chat chat-start animate-in fade-in slide-in-from-bottom-2 duration-300">
              <div class="chat-bubble chat-bubble-success text-sm leading-relaxed whitespace-pre-wrap">{suggestion}</div>
            </div>
        """
        )
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="alert alert-error text-xs">{str(e)}</div>',
            status_code=500,
        )
