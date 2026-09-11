"""Admin-token gate for the /admin routes."""

import hmac
from typing import Annotated

from fastapi import Header

from app.config import settings
from app.errors import Unauthorized


async def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if x_admin_token is None or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise Unauthorized("Invalid admin token")
