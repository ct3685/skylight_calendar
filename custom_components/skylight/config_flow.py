"""Config flow: OAuth2 authorization_code + PKCE (no browser dev tools).

Flow shape:
  step_user          → show the authorize URL as a copyable link, tell the user
                       to sign in and paste back either the full callback URL
                       or just the ?code=... value.
  step_pick_frame    → if the account has >1 frame, choose which one.
  step_reauth / step_reconfigure → same as step_user (reissues authorize URL),
                       and the new sign-in must still see the entry's frame.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse as _up
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SkylightAPI,
    SkylightAPIError,
    SkylightAuthError,
    exchange_authorization_code,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_FINGERPRINT,
    CONF_FRAME_ID,
    CONF_FRAME_NAME,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    SCAN_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for RFC 7636 S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_url(challenge: str) -> str:
    qs = _up.urlencode(
        {
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{qs}"


def _extract_code(raw: str) -> str | None:
    """Accept either a raw code or the full callback URL and return the code."""
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw or raw.startswith("/"):
        try:
            qs = _up.parse_qs(_up.urlparse(raw).query)
        except ValueError:
            return None
        code = qs.get("code", [None])[0]
        return code
    # Strip a stray leading "code=" if the user pasted the fragment.
    if raw.lower().startswith("code="):
        return raw.split("=", 1)[1]
    return raw


class SkylightConfigFlow(ConfigFlow, domain=DOMAIN):
    """OAuth authorization_code + PKCE config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._verifier: str = ""
        self._challenge: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._device_fingerprint: str = ""
        self._frames: list[dict] = []

    # ── Primary user step ────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        # Generate PKCE once per flow so refresh preserves it across form errors.
        if not self._verifier:
            self._verifier, self._challenge = _pkce_pair()

        auth_url = _authorize_url(self._challenge)

        if user_input is not None:
            code = _extract_code(user_input.get(CONF_CODE, ""))
            if not code:
                errors[CONF_CODE] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                # New fingerprint per install so multiple HA instances don't collide.
                self._device_fingerprint = secrets.token_hex(16)
                try:
                    tokens = await exchange_authorization_code(
                        session,
                        code=code,
                        code_verifier=self._verifier,
                        device_fingerprint=self._device_fingerprint,
                    )
                    self._access_token = tokens["access_token"]
                    self._refresh_token = tokens["refresh_token"]
                    api = SkylightAPI(
                        session=session,
                        access_token=self._access_token,
                        refresh_token=self._refresh_token,
                        device_fingerprint=self._device_fingerprint,
                    )
                    self._frames = await api.get_frames()
                except SkylightAuthError as err:
                    _LOGGER.warning("Skylight sign-in failed: %s", err)
                    errors["base"] = "invalid_auth"
                except SkylightAPIError as err:
                    _LOGGER.warning("Skylight sign-in could not complete: %s", err)
                    errors["base"] = "cannot_connect"

                if not errors:
                    if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                        return self._finish_reauth()
                    if not self._frames:
                        errors["base"] = "no_frames"
                    elif len(self._frames) == 1:
                        return await self._create_entry(self._frames[0])
                    else:
                        return await self.async_step_pick_frame()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            description_placeholders={"auth_url": auth_url},
            errors=errors,
        )

    # ── Frame selection ─────────────────────────────────────────────────

    async def async_step_pick_frame(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured_ids = {
            e.data.get(CONF_FRAME_ID) for e in self._async_current_entries()
        }
        available = [f for f in self._frames if f["id"] not in configured_ids]
        if not available:
            return self.async_abort(reason="all_frames_configured")

        if user_input is not None:
            frame_id = user_input[CONF_FRAME_ID]
            frame = next((f for f in available if f["id"] == frame_id), None)
            if frame:
                return await self._create_entry(frame)

        options = {f["id"]: f["name"] for f in available}
        return self.async_show_form(
            step_id="pick_frame",
            data_schema=vol.Schema({vol.Required(CONF_FRAME_ID): vol.In(options)}),
        )

    async def _create_entry(self, frame: dict) -> ConfigFlowResult:
        await self.async_set_unique_id(f"skylight_frame_{frame['id']}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=frame["name"],
            data={
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_DEVICE_FINGERPRINT: self._device_fingerprint,
                CONF_FRAME_ID: frame["id"],
                CONF_FRAME_NAME: frame["name"],
            },
        )

    # ── Reauth / reconfigure ────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_user()

    def _finish_reauth(self) -> ConfigFlowResult:
        entry = (
            self._get_reauth_entry()
            if self.source == SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        if entry.data.get(CONF_FRAME_ID) not in {f["id"] for f in self._frames}:
            return self.async_abort(reason="wrong_account")
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_DEVICE_FINGERPRINT: self._device_fingerprint,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SkylightOptionsFlow:
        return SkylightOptionsFlow()


class SkylightOptionsFlow(OptionsFlow):
    """Tune how often Skylight lists are polled."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        key, default=options.get(key, default)
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                    for key, default in SCAN_INTERVAL_OPTIONS.items()
                }
            ),
        )
