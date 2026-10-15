"""Config flow for SolarEdge ONE (v2 API).

HA's built-in OAuth2 flow cannot be used: SolarEdge does not return the
`state` parameter on the authorization callback, so HA can never match the
callback to the originating flow. Instead the tokens are obtained once out of
band and the refresh token is stored here.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    SOURCE_REAUTH,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTHORIZE_URL,
    CONF_REDIRECT_URI,
    TOKEN_URL,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DAYLIGHT_ONLY,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SITE_ID,
    DEFAULT_DAYLIGHT_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SITE_ID): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): SECRET,
        vol.Required(CONF_AUTHORIZE_URL): str,
        vol.Required(CONF_REDIRECT_URI): str,
        vol.Optional(CONF_REFRESH_TOKEN): SECRET,
    }
)

AUTHORIZE_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})


def extract_code(text: str) -> str:
    """Accept a bare code or a full redirect URL containing ?code=..."""
    text = text.strip()
    if "code=" in text:
        query = urlparse(text).query or text.split("?", 1)[-1]
        codes = parse_qs(query).get("code")
        if codes:
            return codes[0]
    return text


def build_authorize_url(data: Mapping[str, Any]) -> str:
    """Build the URL the user must open to approve access."""
    base = data[CONF_AUTHORIZE_URL]
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode(
        {
            "response_type": "code",
            "client_id": data[CONF_CLIENT_ID],
            "redirect_uri": data[CONF_REDIRECT_URI],
        }
    )


async def exchange_code(
    hass, data: Mapping[str, Any], code: str
) -> str | None:
    """Exchange an authorization code for a refresh token (None on failure)."""
    basic = base64.b64encode(
        f"{data[CONF_CLIENT_ID]}:{data[CONF_CLIENT_SECRET]}".encode()
    ).decode()
    try:
        async with async_get_clientsession(hass).post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": data[CONF_REDIRECT_URI],
            },
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        ) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
    except (aiohttp.ClientError, ValueError):
        return None
    return body.get(CONF_REFRESH_TOKEN)


# A new refresh token makes any cached access token stale.
CLEAR_TOKENS = {CONF_ACCESS_TOKEN: None, CONF_EXPIRES_AT: 0}


class SolarEdgeV2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge ONE."""

    VERSION = 1
    _creds: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SolarEdgeV2OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect site id and credentials."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=SCHEMA)

        await self.async_set_unique_id(str(user_input[CONF_SITE_ID]))
        self._abort_if_unique_id_configured()
        if user_input.get(CONF_REFRESH_TOKEN):
            return self._create(user_input)
        self._creds = user_input
        return await self.async_step_authorize()

    def _create(self, data: Mapping[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"SolarEdge site {data[CONF_SITE_ID]}", data=dict(data)
        )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user approve access and paste the redirected URL."""
        errors: dict[str, str] = {}
        creds = self._creds
        if user_input is not None:
            code = extract_code(user_input["redirect_url"])
            refresh = await exchange_code(self.hass, creds, code) if code else None
            if refresh:
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_REFRESH_TOKEN: refresh, **CLEAR_TOKENS},
                    )
                return self._create({**creds, CONF_REFRESH_TOKEN: refresh})
            errors["base"] = "exchange_failed"
        return self.async_show_form(
            step_id="authorize",
            data_schema=AUTHORIZE_SCHEMA,
            errors=errors,
            description_placeholders={"url": build_authorize_url(creds)},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when SolarEdge rejects the stored tokens."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh refresh token, via browser login when possible."""
        entry = self._get_reauth_entry()
        if user_input is None and entry.data.get(CONF_AUTHORIZE_URL):
            self._creds = dict(entry.data)
            return await self.async_step_authorize()
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_REFRESH_TOKEN): SECRET}),
            )
        return self.async_update_reload_and_abort(
            self._get_reauth_entry(), data_updates={**user_input, **CLEAR_TOKENS}
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the OAuth credentials without removing the integration."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_ID, default=entry.data[CONF_CLIENT_ID]
                    ): str,
                    vol.Required(CONF_CLIENT_SECRET): SECRET,
                    vol.Required(CONF_REFRESH_TOKEN): SECRET,
                }
            )
            return self.async_show_form(step_id="reconfigure", data_schema=schema)
        return self.async_update_reload_and_abort(
            entry, data_updates={**user_input, **CLEAR_TOKENS}
        )


class SolarEdgeV2OptionsFlow(OptionsFlow):
    """Polling settings, editable after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_DAYLIGHT_ONLY,
                    default=opts.get(CONF_DAYLIGHT_ONLY, DEFAULT_DAYLIGHT_ONLY),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
