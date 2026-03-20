from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """
    Serves the home page.
    """
    try:
        templates = request.app.state.templates
        processed_games = request.app.state.client.my_current_games()

        return templates.TemplateResponse(
            "home_page.html",
            {
                "request": request,
                "username": request.app.state.username,
                "version": request.app.state.version,
                "games": processed_games,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"Error retrieving current games: {str(e)}", status_code=500)


@router.get("/game/{id}", response_class=HTMLResponse)
async def game_page(request: Request, id: str):
    """
    Serves the game page by id.
    """
    try:
        templates = request.app.state.templates
        game_data = request.app.state.client.game_detail(id)

        if not game_data:
            return templates.TemplateResponse(
                "game_page.html",
                {
                    "request": request,
                    "username": request.app.state.username,
                    "id": id,
                    "error": "Game not found or no longer active.",
                },
            )

        return templates.TemplateResponse(
            "game_page.html",
            {
                "request": request,
                "username": request.app.state.username,
                "version": request.app.state.version,
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
