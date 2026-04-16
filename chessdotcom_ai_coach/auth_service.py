import base64
import os

import httpx
from authlib.integrations.starlette_client import OAuth

ZITADEL_URL = os.getenv("ZITADEL_URL")
ZITADEL_CLIENT_ID = os.getenv("ZITADEL_CLIENT_ID")
ZITADEL_CLIENT_SECRET = os.getenv("ZITADEL_CLIENT_SECRET")

oauth = OAuth()
oauth.register(
    name="zitadel",
    client_id=ZITADEL_CLIENT_ID,
    client_secret=ZITADEL_CLIENT_SECRET,
    server_metadata_url=f"{ZITADEL_URL}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid profile email"},
)


async def get_user_metadata(client: httpx.AsyncClient, access_token: str, key: str) -> str | None:
    response = await client.get(
        f"{ZITADEL_URL}/auth/v1/users/me/metadata/{key}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    value_b64 = response.json().get("metadata", {}).get("value")
    return base64.b64decode(value_b64).decode() if value_b64 else None


async def set_user_metadata(client: httpx.AsyncClient, access_token: str, key: str, value: str) -> None:
    value_b64 = base64.b64encode(value.encode()).decode()
    response = await client.post(
        f"{ZITADEL_URL}/auth/v1/users/me/metadata/{key}",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"value": value_b64},
    )
    response.raise_for_status()


async def update_display_name(client: httpx.AsyncClient, access_token: str, display_name: str) -> None:
    me = await client.get(
        f"{ZITADEL_URL}/auth/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    me.raise_for_status()
    profile = me.json().get("user", {}).get("human", {}).get("profile", {})

    response = await client.put(
        f"{ZITADEL_URL}/auth/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "firstName": profile.get("firstName", ""),
            "lastName": profile.get("lastName", ""),
            "nickName": profile.get("nickName", ""),
            "displayName": display_name,
            "preferredLanguage": profile.get("preferredLanguage", "en"),
            "gender": profile.get("gender", "GENDER_UNSPECIFIED"),
        },
    )
    response.raise_for_status()
