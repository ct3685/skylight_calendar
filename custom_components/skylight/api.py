"""Minimal Skylight HTTP client: OAuth, frame discovery, and list CRUD only.

Uses fixed Skylight HTTPS hosts. Secrets, request bodies, and server error
responses are deliberately excluded from logs and exception messages.
The underlying service/API is unofficial and may change without notice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    API_VERSION,
    BASE_URL,
    CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_URL,
    USER_AGENT,
)

TokenUpdateCallback = Callable[[str, str, str | None], Awaitable[None]]


class SkylightAPIError(Exception):
    """Skylight operation failed; never contains private server responses."""


class SkylightAuthError(SkylightAPIError):
    """Skylight authorization failed."""


def _segment(value: str) -> str:
    """Only accept simple Skylight identifiers, never URL path fragments."""
    raw = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        raise SkylightAPIError("Invalid Skylight resource identifier")
    return raw


async def _parse_json_response(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await resp.text()
        if not body:
            return {}
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return result
    except (ValueError, UnicodeError) as exc:
        raise SkylightAPIError("Skylight returned an invalid JSON response") from None


class SkylightAPI:
    """Small, reviewable subset of the Skylight web API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        device_fingerprint: str | None = None,
        token_update_cb: TokenUpdateCallback | None = None,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._device_fingerprint = device_fingerprint or ""
        self._token_update_cb = token_update_cb

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> dict[str, Any]:
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise SkylightAPIError("Unsupported Skylight HTTP method")
        if not path.startswith("/api/") or "://" in path:
            raise SkylightAPIError("Invalid Skylight API path")
        url = URL(f"{BASE_URL}{path}")
        for attempt in range(2):
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": USER_AGENT,
                "Skylight-Api-Version": API_VERSION,
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=False,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        await self._refresh_access_token()
                        continue
                    if resp.status == 401:
                        raise SkylightAuthError("Skylight authorization expired")
                    if 300 <= resp.status < 400:
                        raise SkylightAPIError("Unexpected Skylight redirect")
                    if resp.status >= 400:
                        raise SkylightAPIError(
                            f"Skylight {method} request failed (HTTP {resp.status})"
                        )
                    return await _parse_json_response(resp)
            except aiohttp.ClientError:
                raise SkylightAPIError("Could not contact Skylight") from None
        raise SkylightAuthError("Skylight authorization expired")

    async def _refresh_access_token(self) -> None:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": CLIENT_ID,
            "scope": OAUTH_SCOPE,
            "skylight_api_client_device_fingerprint": self._device_fingerprint,
            "skylight_api_client_device_platform": "web",
            "skylight_api_client_device_name": "home-assistant",
            "skylight_api_client_device_os_version": "10",
            "skylight_api_client_device_app_version": "unknown",
            "skylight_api_client_device_hardware": "3",
            "source": "web",
        }
        tokens = await _exchange_token(self._session, payload)
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens.get("refresh_token", self._refresh_token)
        if self._token_update_cb:
            await self._token_update_cb(
                self._access_token, self._refresh_token, self._device_fingerprint
            )

    async def get_frames(self) -> list[dict[str, str]]:
        data = await self._request("GET", "/api/frames")
        frames = []
        for frame in data.get("data", []):
            fid = str(frame.get("id") or "")
            if not fid:
                continue
            attrs = frame.get("attributes") or {}
            name = attrs.get("household_name") or attrs.get("name")
            frames.append({"id": fid, "name": name or f"Skylight Frame {fid}"})
        return frames

    async def get_lists(self, frame_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/frames/{_segment(frame_id)}/lists")

    async def get_list_items(self, frame_id: str, list_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}",
            params={"include": "list_items"},
        )

    async def add_list_item(
        self, frame_id: str, list_id: str, label: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}/list_items",
            json_body={"label": label},
        )

    async def update_list_item(
        self, frame_id: str, list_id: str, item_id: str, attrs: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}"
            f"/list_items/{_segment(item_id)}",
            json_body=attrs,
        )

    async def delete_list_item(
        self, frame_id: str, list_id: str, item_id: str
    ) -> None:
        await self._request(
            "DELETE",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}"
            f"/list_items/{_segment(item_id)}",
        )


async def _exchange_token(
    session: aiohttp.ClientSession, payload: dict[str, str]
) -> dict[str, str]:
    try:
        async with session.post(
            OAUTH_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise SkylightAuthError(
                    f"Skylight OAuth request failed (HTTP {resp.status})"
                )
            tokens = await _parse_json_response(resp)
    except aiohttp.ClientError:
        raise SkylightAuthError("Could not contact Skylight OAuth") from None
    if not tokens.get("access_token"):
        raise SkylightAuthError("Skylight OAuth response omitted access token")
    return tokens


async def exchange_authorization_code(
    session: aiohttp.ClientSession,
    code: str,
    code_verifier: str,
    device_fingerprint: str = "",
) -> dict[str, str]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        "scope": OAUTH_SCOPE,
        "source": "js-mobile",
        "skylight_api_client_device_fingerprint": device_fingerprint,
        "skylight_api_client_device_platform": "web",
        "skylight_api_client_device_name": "home-assistant",
        "skylight_api_client_device_os_version": "unknown",
        "skylight_api_client_device_app_version": "unknown",
        "skylight_api_client_device_hardware": "3",
    }
    tokens = await _exchange_token(session, payload)
    if not tokens.get("refresh_token"):
        raise SkylightAuthError("Skylight OAuth response omitted refresh token")
    return tokens
