import os
from authlib.integrations.starlette_client import OAuth

# Zitadel OIDC Configuration
ZITADEL_DOMAIN = os.getenv("ZITADEL_DOMAIN")
ZITADEL_CLIENT_ID = os.getenv("ZITADEL_CLIENT_ID")
ZITADEL_CLIENT_SECRET = os.getenv("ZITADEL_CLIENT_SECRET")

oauth = OAuth()
oauth.register(
    name="zitadel",
    client_id=ZITADEL_CLIENT_ID,
    client_secret=ZITADEL_CLIENT_SECRET,
    server_metadata_url=f"https://{ZITADEL_DOMAIN}/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid profile email",
    },
)
