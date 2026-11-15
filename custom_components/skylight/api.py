"""Minimal Skylight HTTP client: OAuth, frame discovery, and list item CRUD only.

Uses fixed Skylight HTTPS hosts. Secrets, request bodies, and server error
responses are deliberately excluded from logs and exception messages.
The underlying service/API is unofficial and may change without notice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
import json
import re
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    API_HOST,
    API_VERSION,
    BASE_URL,
    ITEM_STATUS_COMPLETED,
    ITEM_STATUS_PENDING,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TIMEOUT,
    OAUTH_URL,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

TokenUpdateCallback = Callable[[str, str, str | None], Awaitable[None]]

_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_ITEM_STATUSES = frozenset({ITEM_STATUS_COMPLETED, ITEM_STATUS_PENDING})
# The OAuth endpoint answers a rejected/expired grant with 400 (invalid_grant).
_OAUTH_REJECTED = frozenset({400, 401, 403})


class SkylightAPIError(Exception):
    """Skylight operation failed; never contains private server responses."""


class SkylightAuthError(SkylightAPIError):
    """Skylight rejected the stored credentials; re-authentication is needed."""


class SkylightNotFoundError(SkylightAPIError):
    """Skylight answered HTTP 404 for the requested resource."""


def _segment(value: str) -> str:
    """Only accept simple Skylight identifiers, never URL path fragments."""
    raw = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        raise SkylightAPIError("Invalid Skylight resource identifier")
    return raw


def _api_url(path: str) -> URL:
    if not path.startswith("/api/") or "://" in path or ".." in path:
        raise SkylightAPIError("Invalid Skylight API path")
    url = URL(f"{BASE_URL}{path}")
    if url.scheme != "https" or url.host != API_HOST:
        raise SkylightAPIError("Refusing a request outside the Skylight API host")
    return url


def _parse_json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        result = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        result = None
    if result is None:
        raise SkylightAPIError("Skylight returned an invalid JSON response")
    if not isinstance(result, dict):
        raise SkylightAPIError("Skylight returned an unexpected JSON document")
    return result


def _device_fields(fingerprint: str, os_version: str) -> dict[str, str]:
    return {
        "skylight_api_client_device_fingerprint": fingerprint,
        "skylight_api_client_device_platform": "web",
        "skylight_api_client_device_name": "home-assistant",
        "skylight_api_client_device_os_version": os_version,
        "skylight_api_client_device_app_version": "unknown",
        "skylight_api_client_device_hardware": "3",
    }


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
        # Skylight rotates the refresh token on every use, so two concurrent
        # refreshes would race and the loser would be rejected.
        self._refresh_lock = asyncio.Lock()

    async def _send(
        self,
        method: str,
        url: URL,
        token: str,
        params: Mapping[str, str] | None,
        json_body: Any | None,
    ) -> tuple[int, bytes]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
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
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                return resp.status, await resp.read()
        # Raised outside the handlers so no exception context (URLs, hosts)
        # stays attached to the error.
        except TimeoutError:
            failure = "Skylight request timed out"
        except aiohttp.ClientError as err:
            failure = f"Could not contact Skylight ({type(err).__name__})"
        raise SkylightAPIError(failure)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
    ) -> dict[str, Any]:
        if method not in _METHODS:
            raise SkylightAPIError("Unsupported Skylight HTTP method")
        url = _api_url(path)
        for attempt in range(2):
            token = self._access_token
            status, body = await self._send(method, url, token, params, json_body)
            if status == 401 and attempt == 0:
                await self._refresh_access_token(token)
                continue
            if status == 401:
                raise SkylightAuthError("Skylight rejected the refreshed access token")
            if 300 <= status < 400:
                raise SkylightAPIError("Unexpected Skylight redirect")
            if status == 404:
                raise SkylightNotFoundError(f"Skylight {method} returned HTTP 404")
            if status >= 400:
                raise SkylightAPIError(
                    f"Skylight {method} request failed (HTTP {status})"
                )
            return _parse_json(body)
        raise SkylightAuthError("Skylight authorization expired")

    async def _refresh_access_token(self, stale_token: str) -> None:
        async with self._refresh_lock:
            if self._access_token != stale_token:
                return
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": OAUTH_CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "source": "web",
                **_device_fields(self._device_fingerprint, "10"),
            }
            tokens = await _exchange_token(self._session, payload)
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens.get("refresh_token") or self._refresh_token
            if self._token_update_cb:
                await self._token_update_cb(
                    self._access_token, self._refresh_token, self._device_fingerprint
                )

    async def get_frames(self) -> list[dict[str, str]]:
        data = await self._request("GET", "/api/frames")
        records = data.get("data")
        if not isinstance(records, list):
            raise SkylightAPIError("Skylight frame list has an unexpected format")
        frames = []
        for frame in records:
            if not isinstance(frame, dict):
                continue
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
        """The list record with its items in ``included`` (JSON:API)."""
        return await self._request(
            "GET",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}",
            params={"include": "list_items"},
        )

    async def get_list_item_collection(
        self, frame_id: str, list_id: str
    ) -> dict[str, Any]:
        """The bare item collection, with the items in ``data``."""
        return await self._request(
            "GET",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}/list_items",
        )

    async def add_list_item(
        self, frame_id: str, list_id: str, label: str
    ) -> dict[str, Any]:
        # Flat body: list-item writes reject a JSON:API envelope.
        return await self._request(
            "POST",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}/list_items",
            json_body={"label": label},
        )

    async def update_list_item(
        self,
        frame_id: str,
        list_id: str,
        item_id: str,
        *,
        label: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, str] = {}
        if label is not None:
            body["label"] = label
        if status is not None:
            if status not in _ITEM_STATUSES:
                raise SkylightAPIError("Unsupported Skylight list item status")
            body["status"] = status
        if not body:
            raise SkylightAPIError("Nothing to update on the Skylight list item")
        return await self._request(
            "PUT",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}"
            f"/list_items/{_segment(item_id)}",
            json_body=body,
        )

    async def delete_list_item(
        self, frame_id: str, list_id: str, item_id: str
    ) -> None:
        """Delete one item; an item that is already gone counts as deleted."""
        try:
            await self._request(
                "DELETE",
                f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}"
                f"/list_items/{_segment(item_id)}",
            )
        except SkylightNotFoundError:
            return

    async def bulk_delete_list_items(
        self, frame_id: str, list_id: str, item_ids: Iterable[str]
    ) -> None:
        ids = [_segment(item_id) for item_id in item_ids]
        if not ids:
            return
        await self._request(
            "DELETE",
            f"/api/frames/{_segment(frame_id)}/lists/{_segment(list_id)}"
            "/list_items/bulk_destroy",
            json_body={"ids": ids},
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
            timeout=aiohttp.ClientTimeout(total=OAUTH_TIMEOUT),
            allow_redirects=False,
        ) as resp:
            status = resp.status
            body = await resp.read()
    except TimeoutError:
        status, failure = 0, "Skylight OAuth request timed out"
    except aiohttp.ClientError as err:
        status, failure = 0, f"Could not contact Skylight OAuth ({type(err).__name__})"
    if status == 0:
        raise SkylightAPIError(failure)
    if status in _OAUTH_REJECTED:
        raise SkylightAuthError(f"Skylight OAuth rejected the credentials (HTTP {status})")
    if status != 200:
        raise SkylightAPIError(f"Skylight OAuth request failed (HTTP {status})")
    tokens = _parse_json(body)
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        raise SkylightAPIError("Skylight OAuth response omitted the access token")
    if refresh is not None and not isinstance(refresh, str):
        raise SkylightAPIError("Skylight OAuth response has an invalid refresh token")
    return {"access_token": access, **({"refresh_token": refresh} if refresh else {})}


async def exchange_authorization_code(
    session: aiohttp.ClientSession,
    code: str,
    code_verifier: str,
    device_fingerprint: str = "",
) -> dict[str, str]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
        "scope": OAUTH_SCOPE,
        "source": "js-mobile",
        **_device_fields(device_fingerprint, "unknown"),
    }
    tokens = await _exchange_token(session, payload)
    if not tokens.get("refresh_token"):
        raise SkylightAPIError("Skylight OAuth response omitted the refresh token")
    return tokens
