"""Shared fixtures: an in-memory fake of the Skylight list API.

The fake follows the request/response shapes this integration relies on
(flat ``{"label", "status"}`` item writes, JSON:API reads with
``include=list_items``, refresh-token rotation). It is a contract model,
not a recording of the live service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import re
from typing import Any
from unittest.mock import patch

import pytest
from yarl import URL

from custom_components.skylight.const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_FINGERPRINT,
    CONF_FRAME_ID,
    CONF_FRAME_NAME,
    CONF_REFRESH_TOKEN,
)

FRAME_ID = "4242"
ACCESS = "access-token-SECRET-1"
REFRESH = "refresh-token-SECRET-1"


@dataclass
class Call:
    method: str
    url: URL
    kwargs: dict[str, Any]

    @property
    def json(self) -> Any:
        return self.kwargs.get("json")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@dataclass
class FakeSkylight:
    """Stateful fake of the endpoints the integration uses."""

    access_token: str = ACCESS
    refresh_token: str = REFRESH
    lists: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    # Each entry: (predicate(method, path), action) where action is an int
    # status, bytes body, an exception instance, or a callable returning one.
    faults: list[tuple[Callable[[str, str], bool], Any]] = field(default_factory=list)
    ignore_single_delete: bool = False
    omit_relationships: bool = False
    refresh_delay: float = 0
    refresh_count: int = 0
    _next_id: int = 100

    def add_list(self, list_id: str, label: str, kind: str = "shopping") -> None:
        self.lists[list_id] = {"label": label, "kind": kind, "items": {}}

    def add_item(self, list_id: str, label: str, status: str = "pending") -> str:
        self._next_id += 1
        item_id = str(self._next_id)
        self.lists[list_id]["items"][item_id] = {
            "label": label,
            "status": status,
            "position": len(self.lists[list_id]["items"]),
        }
        return item_id

    def fail(self, method: str, path_regex: str, action: Any, times: int = 1) -> None:
        pattern = re.compile(path_regex)
        for _ in range(times):
            self.faults.append(
                (lambda m, p, pat=pattern, meth=method: m == meth and bool(pat.search(p)), action)
            )

    def calls_to(self, method: str, path_regex: str) -> list[Call]:
        pattern = re.compile(path_regex)
        return [c for c in self.calls if c.method == method and pattern.search(c.url.path)]

    # -- transport --------------------------------------------------------

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        return _Pending(self._handle(method.upper(), URL(str(url)), kwargs))

    def post(self, url: Any, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    async def _handle(self, method: str, url: URL, kwargs: dict[str, Any]) -> FakeResponse:
        if kwargs.get("params"):
            url = url.update_query(kwargs["params"])
        self.calls.append(Call(method, url, kwargs))
        for index, (predicate, action) in enumerate(self.faults):
            if predicate(method, url.path):
                del self.faults[index]
                if callable(action) and not isinstance(action, type):
                    action = action()
                if isinstance(action, BaseException):
                    raise action
                if isinstance(action, bytes):
                    return FakeResponse(200, action)
                return FakeResponse(action, b'{"error":"server said SECRET-BODY"}')
        if url.host != "app.ourskylight.com" or url.scheme != "https":
            raise AssertionError(f"unexpected host {url}")
        if url.path == "/oauth/token":
            return await self._oauth(kwargs.get("data") or {})
        if kwargs.get("headers", {}).get("Authorization") != f"Bearer {self.access_token}":
            return FakeResponse(401, b'{"error":"unauthorized"}')
        return self._api(method, url, kwargs.get("json"))

    async def _oauth(self, form: dict[str, str]) -> FakeResponse:
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "good-code":
                return FakeResponse(400, b'{"error":"invalid_grant"}')
        elif form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != self.refresh_token:
                return FakeResponse(400, b'{"error":"invalid_grant"}')
            self.refresh_count += 1
            if self.refresh_delay:
                await asyncio.sleep(self.refresh_delay)
        else:
            return FakeResponse(400, b"{}")
        self.access_token = f"access-token-SECRET-{self.refresh_count + 2}"
        self.refresh_token = f"refresh-token-SECRET-{self.refresh_count + 2}"
        body = {"access_token": self.access_token, "refresh_token": self.refresh_token}
        return FakeResponse(200, json.dumps(body).encode())

    def _api(self, method: str, url: URL, body: Any) -> FakeResponse:
        path = url.path
        if method == "GET" and path == "/api/frames":
            return _json({"data": [{"id": FRAME_ID, "type": "frame",
                                    "attributes": {"household_name": "Test Home"}}]})
        prefix = f"/api/frames/{FRAME_ID}/lists"
        if method == "GET" and path == prefix:
            return _json({"data": [
                {"id": lid, "type": "list", "attributes": {"label": data["label"], "kind": data["kind"]}}
                for lid, data in self.lists.items()
            ]})
        match = re.fullmatch(rf"{prefix}/([^/]+)(?:/list_items(?:/([^/]+))?)?", path)
        if not match or match.group(1) not in self.lists:
            return FakeResponse(404, b"{}")
        list_id, item_id = match.group(1), match.group(2)
        items = self.lists[list_id]["items"]
        is_collection = path.endswith("/list_items")
        if method == "GET" and item_id is None and not is_collection:
            records = [_item_record(i, d) for i, d in items.items()]
            data: dict[str, Any] = {"id": list_id, "type": "list", "attributes": {"label": self.lists[list_id]["label"]}}
            if not self.omit_relationships:
                data["relationships"] = {"list_items": {"data": [{"id": i, "type": "list_item"} for i in items]}}
            doc: dict[str, Any] = {"data": data}
            if records:
                doc["included"] = records
            return _json(doc)
        if method == "GET" and is_collection:
            return _json({"data": [_item_record(i, d) for i, d in items.items()]})
        if method == "POST" and is_collection:
            if not isinstance(body, dict) or not body.get("label") or "data" in body:
                return FakeResponse(422, b'{"errors":["Label can\'t be blank"]}')
            new_id = self.add_item(list_id, body["label"])
            return FakeResponse(201, json.dumps({"data": _item_record(new_id, items[new_id])}).encode())
        if method == "DELETE" and item_id == "bulk_destroy":
            for i in (body or {}).get("ids", []):
                items.pop(str(i), None)
            return FakeResponse(200, b"")
        if item_id is None or item_id not in items:
            return FakeResponse(404, b"{}")
        if method == "PUT":
            if not isinstance(body, dict) or "data" in body or set(body) - {"label", "status", "position"}:
                return FakeResponse(422, b"{}")
            if "status" in body and body["status"] not in ("pending", "completed"):
                return FakeResponse(422, b"{}")
            items[item_id].update(body)
            return _json({"data": _item_record(item_id, items[item_id])})
        if method == "DELETE":
            if not self.ignore_single_delete:
                items.pop(item_id)
            return FakeResponse(204, b"")
        return FakeResponse(405, b"{}")


class _Pending:
    """aiohttp-style async context manager wrapping the fake handler."""

    def __init__(self, coro: Any) -> None:
        self._coro = coro

    async def __aenter__(self) -> FakeResponse:
        return await self._coro

    async def __aexit__(self, *exc: object) -> None:
        return None


def _item_record(item_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": item_id, "type": "list_item", "attributes": dict(data)}


def _json(doc: Any) -> FakeResponse:
    return FakeResponse(200, json.dumps(doc).encode())


@pytest.fixture
def skylight() -> FakeSkylight:
    fake = FakeSkylight()
    fake.add_list("1", "Grocery")
    fake.add_list("2", "Costco")
    return fake


@pytest.fixture
def entry_data() -> dict[str, str]:
    return {
        CONF_ACCESS_TOKEN: ACCESS,
        CONF_REFRESH_TOKEN: REFRESH,
        CONF_DEVICE_FINGERPRINT: "fp",
        CONF_FRAME_ID: FRAME_ID,
        CONF_FRAME_NAME: "Test Home",
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    return None


@pytest.fixture
def patch_session(skylight: FakeSkylight) -> Any:
    with (
        patch("custom_components.skylight.async_get_clientsession", return_value=skylight),
        patch("custom_components.skylight.config_flow.async_get_clientsession", return_value=skylight),
    ):
        yield skylight
