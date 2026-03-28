from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from chessdotcom_ai_coach.dependencies import ClientDep, auth_required
from chessdotcom_ai_coach.user import User
from chessdotcom_ai_coach.game_service import get_best_move

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: User = Depends(auth_required),
    client: ClientDep = None,
):
    """
    Serves the home page.
    """
    try:
        templates = request.app.state.templates
        processed_games = client.my_current_games() if client else []

        return templates.TemplateResponse(
            "home_page.html",
            {
                "request": request,
                "username": user.username,
                "version": getattr(request.app.state, "version", "0.1.0"),
                "games": processed_games,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving current games: {str(e)}", status_code=500)


@router.get("/game/{id}", response_class=HTMLResponse)
async def game_page(
    request: Request,
    id: str,
    user: User = Depends(auth_required),
    client: ClientDep = None,
):
    """
    Serves the game page by id.
    """
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
                    "username": user.username,
                    "id": id,
                    "error": "Game not found or no longer active.",
                },
            )

        return templates.TemplateResponse(
            "game_page.html",
            {
                "request": request,
                "username": user.username,
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


@router.post("/game/{id}/suggest", response_class=HTMLResponse)
async def get_suggestion(
    request: Request,
    id: str,
    fen: str = Form(...),
    pgn: str = Form(None),
    user: User = Depends(auth_required),
):
    """
    Returns the AI coach suggestion for the given position.
    """
    try:
        suggestion = await get_best_move(fen, pgn)

        # Return the suggestion as a chat bubble
        # whitespace-pre-wrap is used to preserve formatting from LC0 analysis
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
