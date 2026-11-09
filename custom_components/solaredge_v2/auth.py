"""SolarEdge OAuth token exchange."""
from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    TOKEN_URL,
)


class InvalidAuth(Exception):
    """SolarEdge rejected the OAuth credentials."""


class TokenExchangeError(Exception):
    """The token endpoint could not complete the request."""


async def async_refresh_token(
    session: aiohttp.ClientSession, credentials: Mapping[str, Any]
) -> dict[str, Any]:
    """Exchange a refresh token and return the tokens to persist."""
    basic = base64.b64encode(
        f"{credentials[CONF_CLIENT_ID]}:{credentials[CONF_CLIENT_SECRET]}".encode()
    ).decode()
    try:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials[CONF_REFRESH_TOKEN],
            },
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        ) as resp:
            if resp.status in (400, 401, 403):
                raise InvalidAuth(f"Token refresh rejected ({resp.status})")
            if resp.status != 200:
                raise TokenExchangeError(f"Token endpoint returned {resp.status}")
            body = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise TokenExchangeError("Could not retrieve a valid token response") from err

    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise TokenExchangeError("Token response is missing an access token")
    if not body["access_token"]:
        raise TokenExchangeError("Token response contains an empty access token")
    expires_in = body.get("expires_in", 7200)
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise TokenExchangeError("Token response contains an invalid expiry")
    refresh_token = body.get("refresh_token")
    if refresh_token is not None and (
        not isinstance(refresh_token, str) or not refresh_token
    ):
        raise TokenExchangeError("Token response contains an invalid refresh token")

    return {
        CONF_ACCESS_TOKEN: body["access_token"],
        CONF_EXPIRES_AT: time.time() + expires_in,
        CONF_REFRESH_TOKEN: refresh_token or credentials[CONF_REFRESH_TOKEN],
    }
