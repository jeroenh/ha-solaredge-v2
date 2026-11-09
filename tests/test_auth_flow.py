"""Tests for credential validation without a running Home Assistant instance."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp


class FakeConfigFlow:
    """Provide the Home Assistant flow methods exercised by these tests."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def _get_reauth_entry(self):
        return self.entry

    def _get_reconfigure_entry(self):
        return self.entry

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_update_reload_and_abort(self, entry, *, data_updates):
        entry.data = {**entry.data, **data_updates}
        return {"type": "abort", "data_updates": data_updates}


class Selector:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, value):
        return value


def load_modules():
    """Load the flow with lightweight Home Assistant stand-ins."""
    root = Path(__file__).resolve().parents[1] / "custom_components" / "solaredge_v2"
    package = types.ModuleType("test_solaredge_v2")
    package.__path__ = [str(root)]
    modules = {
        "test_solaredge_v2": package,
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.config_entries": types.ModuleType("homeassistant.config_entries"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": types.ModuleType(
            "homeassistant.helpers.aiohttp_client"
        ),
        "homeassistant.helpers.selector": types.ModuleType(
            "homeassistant.helpers.selector"
        ),
    }
    entries = modules["homeassistant.config_entries"]
    entries.ConfigFlow = FakeConfigFlow
    entries.ConfigEntry = object
    entries.ConfigFlowResult = dict
    entries.OptionsFlow = object
    modules["homeassistant.core"].callback = lambda fn: fn
    helpers = modules["homeassistant.helpers"]
    helpers.aiohttp_client = modules["homeassistant.helpers.aiohttp_client"]
    selector = modules["homeassistant.helpers.selector"]
    for name in (
        "BooleanSelector",
        "NumberSelector",
        "NumberSelectorConfig",
        "TextSelector",
        "TextSelectorConfig",
    ):
        setattr(selector, name, Selector)
    selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
    selector.TextSelectorType = types.SimpleNamespace(PASSWORD="password")
    with patch.dict(sys.modules, modules):
        auth = importlib.import_module("test_solaredge_v2.auth")
        flow = importlib.import_module("test_solaredge_v2.config_flow")
    return auth, flow


auth, config_flow = load_modules()


class Response:
    def __init__(self, status, body=None, error=None):
        self.status = status
        self.body = body
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if self.error:
            raise self.error
        return self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


class AuthFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.credentials = {
            "client_id": "new-client",
            "client_secret": "new-secret",
            "refresh_token": "new-refresh",
        }

    async def test_token_exchange_returns_rotated_credentials(self):
        session = Session(Response(200, {
            "access_token": "access",
            "refresh_token": "rotated",
            "expires_in": 3600,
        }))
        with patch.object(auth.time, "time", return_value=100):
            tokens = await auth.async_refresh_token(session, self.credentials)
        self.assertEqual(tokens, {
            "access_token": "access", "refresh_token": "rotated", "expires_at": 3700
        })
        self.assertEqual(session.request[1]["data"]["refresh_token"], "new-refresh")
        self.assertEqual(
            session.request[1]["headers"]["Authorization"],
            "Basic bmV3LWNsaWVudDpuZXctc2VjcmV0",
        )

    async def test_token_exchange_keeps_unrotated_token(self):
        session = Session(Response(200, {"access_token": "access"}))
        tokens = await auth.async_refresh_token(session, self.credentials)
        self.assertEqual(tokens["refresh_token"], "new-refresh")

    async def test_token_exchange_errors(self):
        for response, error in (
            (Response(400), auth.InvalidAuth),
            (Response(401), auth.InvalidAuth),
            (Response(403), auth.InvalidAuth),
            (Response(429), auth.TokenExchangeError),
            (Response(500), auth.TokenExchangeError),
            (Response(200, error=ValueError("bad JSON")), auth.TokenExchangeError),
            (Response(200, error=aiohttp.ClientConnectionError()), auth.TokenExchangeError),
            (Response(200, error=TimeoutError()), auth.TokenExchangeError),
            (Response(200, {}), auth.TokenExchangeError),
            (
                Response(200, {"access_token": "access", "expires_in": "bad"}),
                auth.TokenExchangeError,
            ),
        ):
            with self.subTest(status=response.status, error=error):
                with self.assertRaises(error):
                    await auth.async_refresh_token(Session(response), self.credentials)

    async def test_both_flows_validate_and_preserve_entry(self):
        for step in ("reauth_confirm", "reconfigure"):
            with self.subTest(step=step):
                flow = config_flow.SolarEdgeV2ConfigFlow()
                flow.hass = object()
                flow.entry = types.SimpleNamespace(data={
                    "site_id": "site", "client_id": "old-client",
                    "client_secret": "old-secret", "refresh_token": "old-refresh",
                    "access_token": "old-access", "expires_at": 1,
                })
                session = Session(Response(200, {
                    "access_token": "access", "refresh_token": "rotated",
                    "expires_in": 3600,
                }))
                with patch.object(
                    config_flow.aiohttp_client, "async_get_clientsession",
                    return_value=session, create=True,
                ):
                    form = await getattr(flow, f"async_step_{step}")()
                    self.assertEqual(form["type"], "form")
                    schema_keys = {str(key) for key in form["data_schema"].schema}
                    self.assertEqual(schema_keys, set(self.credentials))
                    result = await getattr(flow, f"async_step_{step}")(self.credentials)
                self.assertEqual(result["type"], "abort")
                self.assertEqual(flow.entry.data["site_id"], "site")
                self.assertEqual(flow.entry.data["client_id"], "new-client")
                self.assertEqual(flow.entry.data["client_secret"], "new-secret")
                self.assertEqual(flow.entry.data["refresh_token"], "rotated")
                self.assertEqual(flow.entry.data["access_token"], "access")

    async def test_failures_show_error_without_changing_entry(self):
        for step in ("reauth_confirm", "reconfigure"):
            for status, error in ((401, "invalid_auth"), (503, "cannot_connect")):
                with self.subTest(step=step, status=status):
                    flow = config_flow.SolarEdgeV2ConfigFlow()
                    flow.hass = object()
                    original = {"client_id": "old", "refresh_token": "old-token"}
                    flow.entry = types.SimpleNamespace(data=original.copy())
                    with patch.object(
                        config_flow.aiohttp_client, "async_get_clientsession",
                        return_value=Session(Response(status)), create=True,
                    ):
                        result = await getattr(flow, f"async_step_{step}")(
                            self.credentials
                        )
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["errors"], {"base": error})
                    self.assertEqual(flow.entry.data, original)
