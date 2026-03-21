from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from chessdotcom_ai_coach.dependencies import ClientDep, auth_required

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    username: str = Depends(auth_required),
    client: ClientDep = Depends(),
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
                "username": username,
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
    username: str = Depends(auth_required),
    client: ClientDep = Depends(),
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
                    "username": username,
                    "id": id,
                    "error": "Game not found or no longer active.",
                },
            )

        return templates.TemplateResponse(
            "game_page.html",
            {
                "request": request,
                "username": username,
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
