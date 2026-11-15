"""Contract tests for the Skylight HTTP client against the fake API."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from custom_components.skylight.api import (
    SkylightAPI,
    SkylightAPIError,
    SkylightAuthError,
    exchange_authorization_code,
)
from custom_components.skylight.coordinator import fetch_list_items

from .conftest import ACCESS, FRAME_ID, REFRESH, FakeSkylight

SECRETS = ("SECRET", "good-code", "verifier-value")


def make_api(fake: FakeSkylight, persisted: list | None = None) -> SkylightAPI:
    async def _persist(access: str, refresh: str, fp: str | None) -> None:
        if persisted is not None:
            persisted.append((access, refresh, fp))

    return SkylightAPI(fake, ACCESS, REFRESH, "fp", _persist)  # type: ignore[arg-type]


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# -- request shapes -----------------------------------------------------


async def test_add_item_sends_flat_label(skylight: FakeSkylight) -> None:
    api = make_api(skylight)
    await api.add_list_item(FRAME_ID, "1", "Milk")
    (call,) = skylight.calls_to("POST", r"/list_items$")
    assert call.json == {"label": "Milk"}
    assert [i["label"] for i in skylight.lists["1"]["items"].values()] == ["Milk"]


async def test_update_sends_only_given_fields(skylight: FakeSkylight) -> None:
    item = skylight.add_item("1", "Milk")
    api = make_api(skylight)
    await api.update_list_item(FRAME_ID, "1", item, status="completed")
    await api.update_list_item(FRAME_ID, "1", item, label="Oat milk")
    bodies = [c.json for c in skylight.calls_to("PUT", r"/list_items/")]
    assert bodies == [{"status": "completed"}, {"label": "Oat milk"}]
    assert skylight.lists["1"]["items"][item] == {
        "label": "Oat milk", "status": "completed", "position": 0,
    }


async def test_update_rejects_unknown_status_before_sending(skylight: FakeSkylight) -> None:
    api = make_api(skylight)
    with pytest.raises(SkylightAPIError):
        await api.update_list_item(FRAME_ID, "1", "101", status="complete")
    with pytest.raises(SkylightAPIError):
        await api.update_list_item(FRAME_ID, "1", "101")
    assert not skylight.calls


async def test_every_request_is_https_skylight_without_redirects(skylight: FakeSkylight) -> None:
    item = skylight.add_item("1", "Milk")
    skylight.access_token = "rotated"
    api = make_api(skylight)
    await api.get_lists(FRAME_ID)
    await api.update_list_item(FRAME_ID, "1", item, status="completed")
    await api.delete_list_item(FRAME_ID, "1", item)
    assert skylight.calls
    for call in skylight.calls:
        assert call.url.scheme == "https"
        assert call.url.host == "app.ourskylight.com"
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["timeout"].total and call.kwargs["timeout"].total <= 20


@pytest.mark.parametrize("bad", ["../2", "1/list_items", "1?x=1", "", "a" * 200, "%2e%2e"])
async def test_identifiers_cannot_alter_the_path(skylight: FakeSkylight, bad: str) -> None:
    api = make_api(skylight)
    with pytest.raises(SkylightAPIError, match="identifier"):
        await api.get_list_items(FRAME_ID, bad)
    with pytest.raises(SkylightAPIError, match="identifier"):
        await api.delete_list_item(FRAME_ID, "1", bad)
    assert not skylight.calls


async def test_redirect_is_refused(skylight: FakeSkylight) -> None:
    skylight.fail("GET", r"/lists$", 302)
    with pytest.raises(SkylightAPIError, match="redirect"):
        await make_api(skylight).get_lists(FRAME_ID)


# -- delete -------------------------------------------------------------


async def test_delete_is_idempotent(skylight: FakeSkylight) -> None:
    item = skylight.add_item("1", "Milk")
    api = make_api(skylight)
    await api.delete_list_item(FRAME_ID, "1", item)
    await api.delete_list_item(FRAME_ID, "1", item)
    assert skylight.lists["1"]["items"] == {}


async def test_bulk_delete_body(skylight: FakeSkylight) -> None:
    a = skylight.add_item("1", "Milk")
    b = skylight.add_item("1", "Eggs")
    await make_api(skylight).bulk_delete_list_items(FRAME_ID, "1", [a, b])
    (call,) = skylight.calls_to("DELETE", r"/bulk_destroy$")
    assert call.json == {"ids": [a, b]}
    assert skylight.lists["1"]["items"] == {}


# -- authentication -----------------------------------------------------


async def test_expired_token_refreshes_once_and_persists_rotation(skylight: FakeSkylight) -> None:
    skylight.access_token = "server-rotated"
    persisted: list = []
    api = make_api(skylight, persisted)
    await api.get_lists(FRAME_ID)
    assert skylight.refresh_count == 1
    assert persisted == [(skylight.access_token, skylight.refresh_token, "fp")]
    (refresh_call,) = skylight.calls_to("POST", r"^/oauth/token$")
    assert refresh_call.kwargs["data"]["refresh_token"] == REFRESH
    assert refresh_call.kwargs["data"]["grant_type"] == "refresh_token"
    # The rotated refresh token is used next time.
    skylight.access_token = "server-rotated-again"
    await api.get_lists(FRAME_ID)
    assert skylight.refresh_count == 2


async def test_concurrent_requests_share_one_refresh(skylight: FakeSkylight) -> None:
    skylight.access_token = "server-rotated"
    skylight.refresh_delay = 0.05
    api = make_api(skylight)
    await asyncio.gather(*(api.get_lists(FRAME_ID) for _ in range(5)))
    assert skylight.refresh_count == 1


async def test_revoked_refresh_token_is_an_auth_error(skylight: FakeSkylight) -> None:
    skylight.access_token = "server-rotated"
    skylight.refresh_token = "someone-else"
    with pytest.raises(SkylightAuthError) as err:
        await make_api(skylight).get_lists(FRAME_ID)
    assert_no_secrets(str(err.value))


async def test_still_unauthorized_after_refresh_is_an_auth_error(skylight: FakeSkylight) -> None:
    skylight.access_token = "server-rotated"
    skylight.fail("GET", r"/lists$", 401, times=2)
    with pytest.raises(SkylightAuthError):
        await make_api(skylight).get_lists(FRAME_ID)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_oauth_outage_is_not_an_auth_error(skylight: FakeSkylight, status: int) -> None:
    skylight.access_token = "server-rotated"
    skylight.fail("POST", r"^/oauth/token$", status)
    with pytest.raises(SkylightAPIError) as err:
        await make_api(skylight).get_lists(FRAME_ID)
    assert not isinstance(err.value, SkylightAuthError)


async def test_code_exchange(skylight: FakeSkylight) -> None:
    tokens = await exchange_authorization_code(skylight, "good-code", "verifier-value", "fp")  # type: ignore[arg-type]
    assert tokens["access_token"] == skylight.access_token
    form = skylight.calls[0].kwargs["data"]
    assert form["code_verifier"] == "verifier-value"
    assert form["grant_type"] == "authorization_code"
    with pytest.raises(SkylightAuthError) as err:
        await exchange_authorization_code(skylight, "bad-code", "verifier-value")  # type: ignore[arg-type]
    assert_no_secrets(str(err.value))


async def test_oauth_without_refresh_token_is_rejected(skylight: FakeSkylight) -> None:
    skylight.fail("POST", r"^/oauth/token$", b'{"access_token":"x-SECRET"}')
    with pytest.raises(SkylightAPIError) as err:
        await exchange_authorization_code(skylight, "good-code", "v")  # type: ignore[arg-type]
    assert_no_secrets(str(err.value))


# -- failure modes ------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (TimeoutError(), "timed out"),
        (aiohttp.ClientConnectionError("SECRET host details"), "Could not contact"),
        (500, "HTTP 500"),
        (503, "HTTP 503"),
        (429, "HTTP 429"),
        (b"{not json", "invalid JSON"),
        (b"[1, 2]", "unexpected JSON"),
        (b"\xff\xfe", "invalid JSON"),
    ],
)
async def test_failures_raise_sanitized_api_errors(
    skylight: FakeSkylight, action: object, message: str
) -> None:
    skylight.fail("GET", r"/lists$", action)
    with pytest.raises(SkylightAPIError, match=message) as err:
        await make_api(skylight).get_lists(FRAME_ID)
    text = str(err.value) + repr(err.value.__cause__) + repr(err.value.__context__)
    assert_no_secrets(text)
    assert "SECRET-BODY" not in text
    assert err.value.__cause__ is None


async def test_error_messages_never_contain_request_data(skylight: FakeSkylight) -> None:
    skylight.fail("POST", r"/list_items$", 422)
    with pytest.raises(SkylightAPIError) as err:
        await make_api(skylight).add_list_item(FRAME_ID, "1", "Private SECRET item")
    assert_no_secrets(str(err.value))
    assert "Private" not in str(err.value)


# -- reads --------------------------------------------------------------


async def test_read_items_sorted_with_status(skylight: FakeSkylight) -> None:
    skylight.add_item("1", "Milk")
    skylight.add_item("1", "Eggs", "completed")
    items = await fetch_list_items(make_api(skylight), FRAME_ID, "1")
    assert [(i["name"], i["status"]) for i in items] == [
        ("Milk", "pending"), ("Eggs", "completed"),
    ]


async def test_empty_list_without_relationships_is_confirmed(skylight: FakeSkylight) -> None:
    skylight.omit_relationships = True
    items = await fetch_list_items(make_api(skylight), FRAME_ID, "1")
    assert items == []
    assert skylight.calls_to("GET", r"/lists/1/list_items$")
