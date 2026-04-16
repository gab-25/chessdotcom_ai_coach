from typing import Annotated

from fastapi import Depends, Request

from chessdotcom_ai_coach.client import Client


class AuthRedirectException(Exception):
    pass


async def get_current_user(request: Request) -> dict | None:
    if not request.session.get("sub"):
        return None
    return {
        "sub": request.session["sub"],
        "display_name": request.session.get("display_name", ""),
        "access_token": request.session.get("access_token", ""),
    }


def auth_required(user: Annotated[dict | None, Depends(get_current_user)]):
    if not user:
        raise AuthRedirectException()
    return user


def get_client(request: Request, user: Annotated[dict | None, Depends(get_current_user)]):
    if not user:
        return None
    chess_username = request.session.get("chess_username")
    if not chess_username:
        return None
    return Client(username=chess_username)


ClientDep = Annotated[Client | None, Depends(get_client)]
