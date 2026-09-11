"""Admin-token gate for the /admin routes."""

import hmac
from typing import Annotated

from fastapi import Header

from app.config import settings
from app.errors import Unauthorized


async def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    # Starlette decodes headers latin-1, so compare bytes: compare_digest raises on non-ASCII str.
    if x_admin_token is None or not hmac.compare_digest(
        x_admin_token.encode("latin-1"), settings.admin_token.encode()
    ):
        raise Unauthorized("Invalid admin token")
