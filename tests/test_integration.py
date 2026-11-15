"""Home Assistant integration tests: setup, todo services, outages, auth."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.skylight.const import (
    CONF_ACCESS_TOKEN,
    CONF_LISTS_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .conftest import FRAME_ID, FakeSkylight

GROCERY = "todo.test_home_grocery"
COSTCO = "todo.test_home_costco"

pytestmark = pytest.mark.usefixtures("patch_session")


async def setup(hass: HomeAssistant, entry_data: dict[str, str], **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options=options,
        unique_id=f"skylight_frame_{FRAME_ID}",
        version=2,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def get_items(hass: HomeAssistant, entity_id: str = GROCERY) -> list[dict[str, Any]]:
    resp = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": entity_id},
        blocking=True, return_response=True,
    )
    return resp[entity_id]["items"]


async def todo(hass: HomeAssistant, service: str, data: dict[str, Any], entity_id: str = GROCERY) -> None:
    await hass.services.async_call(
        "todo", service, data, target={"entity_id": entity_id}, blocking=True
    )


async def poll(hass: HomeAssistant, seconds: int = 130) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# -- setup & entities ---------------------------------------------------


async def test_one_todo_entity_per_list(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.add_item("1", "Milk")
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id))
    assert ids == [COSTCO, GROCERY]
    state = hass.states.get(GROCERY)
    assert state.state == "1"
    assert state.attributes["skylight_list_id"] == "1"
    assert state.attributes["skylight_kind"] == "shopping"
    assert hass.services.async_services().get(DOMAIN) is None


async def test_unload(hass: HomeAssistant, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_startup_outage_retries_setup(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.fail("GET", r"/lists$", 503)
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_new_skylight_list_appears_on_poll(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    await setup(hass, entry_data)
    skylight.add_list("3", "Hardware")
    await poll(hass)
    assert hass.states.get("todo.test_home_hardware") is not None


# -- item round trips ---------------------------------------------------


async def test_read_items(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.add_item("1", "Milk")
    skylight.add_item("1", "Eggs", "completed")
    await setup(hass, entry_data)
    items = await get_items(hass)
    assert [(i["summary"], i["status"]) for i in items] == [
        ("Milk", "needs_action"), ("Eggs", "completed"),
    ]


async def test_add_item(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    await setup(hass, entry_data)
    await todo(hass, "add_item", {"item": "  Milk "})
    assert [i["summary"] for i in await get_items(hass)] == ["Milk"]
    assert skylight.calls_to("POST", r"/list_items$")[0].json == {"label": "Milk"}


async def test_complete_and_uncomplete(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    await todo(hass, "update_item", {"item": uid, "status": "completed"})
    assert skylight.lists["1"]["items"][uid]["status"] == "completed"
    assert (await get_items(hass))[0]["status"] == "completed"
    await todo(hass, "update_item", {"item": "Milk", "status": "needs_action"})
    assert skylight.lists["1"]["items"][uid]["status"] == "pending"
    assert [c.json for c in skylight.calls_to("PUT", r"/list_items/")] == [
        {"status": "completed"}, {"status": "pending"},
    ]


async def test_rename(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    await todo(hass, "update_item", {"item": uid, "rename": "Oat milk"})
    assert skylight.lists["1"]["items"][uid]["label"] == "Oat milk"
    assert skylight.calls_to("PUT", r"/list_items/")[0].json == {"label": "Oat milk"}


async def test_rapid_writes_within_refresh_cooldown_are_not_dropped(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data
) -> None:
    await setup(hass, entry_data)
    await todo(hass, "add_item", {"item": "Milk"})
    (uid,) = skylight.lists["1"]["items"]
    assert [i["summary"] for i in await get_items(hass)] == ["Milk"]
    await todo(hass, "update_item", {"item": uid, "status": "completed"})
    await todo(hass, "update_item", {"item": uid, "status": "needs_action"})
    await todo(hass, "update_item", {"item": uid, "rename": "Oat milk"})
    await todo(hass, "update_item", {"item": uid, "rename": "Milk"})
    await todo(hass, "add_item", {"item": "Eggs"})
    assert [c.json for c in skylight.calls_to("PUT", r"/list_items/")] == [
        {"status": "completed"}, {"status": "pending"}, {"label": "Oat milk"}, {"label": "Milk"},
    ]
    assert skylight.lists["1"]["items"][uid] == {"label": "Milk", "status": "pending", "position": 0}
    assert [(i["summary"], i["status"]) for i in await get_items(hass)] == [
        ("Milk", "needs_action"), ("Eggs", "needs_action"),
    ]


async def test_unchanged_update_sends_nothing(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    await todo(hass, "update_item", {"item": uid, "status": "needs_action"})
    assert not skylight.calls_to("PUT", r"/list_items/")


async def test_delete(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    keep = skylight.add_item("1", "Eggs")
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    await todo(hass, "remove_item", {"item": [uid]})
    assert list(skylight.lists["1"]["items"]) == [keep]
    assert [i["summary"] for i in await get_items(hass)] == ["Eggs"]
    assert not skylight.calls_to("DELETE", r"bulk_destroy")


async def test_delete_falls_back_to_bulk_destroy(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.ignore_single_delete = True
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    await todo(hass, "remove_item", {"item": [uid]})
    assert skylight.lists["1"]["items"] == {}
    assert skylight.calls_to("DELETE", r"bulk_destroy")[0].json == {"ids": [uid]}


async def test_delete_that_never_happens_is_an_error(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.ignore_single_delete = True
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    skylight.fail("DELETE", r"bulk_destroy", b"")  # accepted, but nothing removed
    with pytest.raises(HomeAssistantError, match="did not delete"):
        await todo(hass, "remove_item", {"item": [uid]})
    assert uid in skylight.lists["1"]["items"]


PARTIAL_READ = b'{"data":{"id":"1","relationships":{"list_items":{"data":[{"id":"7"}]}}}}'
VERIFY_FAILURES = [503, TimeoutError(), b"{broken", PARTIAL_READ]


async def test_delete_is_verified_and_leaves_other_items(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data
) -> None:
    keep = skylight.add_item("1", "Eggs", "completed")
    other_list = skylight.add_item("2", "Paper towels")
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    reads_before = len(skylight.calls_to("GET", r"/lists/1$"))
    await todo(hass, "remove_item", {"item": [uid]})
    assert skylight.lists["1"]["items"] == {keep: {"label": "Eggs", "status": "completed", "position": 0}}
    assert list(skylight.lists["2"]["items"]) == [other_list]
    assert [c.url.path.rsplit("/", 1)[1] for c in skylight.calls_to("DELETE", r"")] == [uid]
    # The verification read is published directly; no extra full refresh.
    assert len(skylight.calls_to("GET", r"/lists/1$")) == reads_before + 1
    assert [i["summary"] for i in await get_items(hass)] == ["Eggs"]


@pytest.mark.parametrize("failure", VERIFY_FAILURES)
async def test_failed_verification_is_an_error_not_a_success(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data, failure: object
) -> None:
    keep = skylight.add_item("1", "Eggs")
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    skylight.ignore_single_delete = True
    skylight.fail("GET", r"/lists/1$", failure)
    with pytest.raises(HomeAssistantError, match="Could not confirm") as err:
        await todo(hass, "remove_item", {"item": [uid]})
    assert "SECRET" not in str(err.value)
    # Nothing further is attempted on an unverified state.
    assert not skylight.calls_to("DELETE", r"bulk_destroy")
    assert set(skylight.lists["1"]["items"]) == {keep, uid}
    # The entity refreshed and still shows the undeleted item.
    assert sorted(i["summary"] for i in await get_items(hass)) == ["Eggs", "Milk"]


@pytest.mark.parametrize("failure", VERIFY_FAILURES)
async def test_failed_verification_after_bulk_fallback_is_an_error(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data, failure: object
) -> None:
    keep = skylight.add_item("1", "Eggs")
    uid = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    skylight.ignore_single_delete = True

    def bulk_then_fail_reads() -> bytes:
        skylight.lists["1"]["items"].pop(uid)
        skylight.fail("GET", r"/lists/1$", failure)
        return b""

    skylight.fail("DELETE", r"bulk_destroy", bulk_then_fail_reads)
    with pytest.raises(HomeAssistantError, match="Could not confirm"):
        await todo(hass, "remove_item", {"item": [uid]})
    assert list(skylight.lists["1"]["items"]) == [keep]


@pytest.mark.parametrize("failure", [503, 500, 429, TimeoutError()])
async def test_outage_during_delete_then_recovery(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data, failure: object
) -> None:
    keep = skylight.add_item("1", "Eggs")
    first = skylight.add_item("1", "Milk")
    second = skylight.add_item("1", "Bread")
    await setup(hass, entry_data)
    skylight.fail("DELETE", rf"/list_items/{second}$", failure)
    with pytest.raises(HomeAssistantError):
        await todo(hass, "remove_item", {"item": [first, second]})
    # The first delete went through; the failed one and the unrelated item remain.
    assert set(skylight.lists["1"]["items"]) == {keep, second}
    assert not skylight.calls_to("DELETE", r"bulk_destroy")
    assert sorted(i["summary"] for i in await get_items(hass)) == ["Bread", "Eggs"]
    # Retrying after the outage succeeds and is verified.
    await todo(hass, "remove_item", {"item": [second]})
    assert list(skylight.lists["1"]["items"]) == [keep]
    assert [i["summary"] for i in await get_items(hass)] == ["Eggs"]


async def test_duplicate_names_target_by_uid(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    first = skylight.add_item("1", "Milk")
    second = skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    assert len(await get_items(hass)) == 2
    await todo(hass, "update_item", {"item": second, "status": "completed"})
    assert skylight.lists["1"]["items"][first]["status"] == "pending"
    assert skylight.lists["1"]["items"][second]["status"] == "completed"


async def test_concurrent_updates(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    uids = [skylight.add_item("1", f"Item {n}") for n in range(5)]
    await setup(hass, entry_data)
    skylight.access_token = "expired-meanwhile"
    skylight.refresh_delay = 0.02
    await asyncio.gather(*(
        todo(hass, "update_item", {"item": uid, "status": "completed"}) for uid in uids
    ))
    assert all(skylight.lists["1"]["items"][u]["status"] == "completed" for u in uids)
    assert skylight.refresh_count == 1


async def test_write_failure_is_reported_not_swallowed(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    await setup(hass, entry_data)
    skylight.fail("POST", r"/list_items$", 500)
    with pytest.raises(HomeAssistantError, match="HTTP 500"):
        await todo(hass, "add_item", {"item": "Milk"})


# -- outages must not look like empty lists -----------------------------


@pytest.mark.parametrize(
    "action",
    [503, TimeoutError(), b"{broken", b'{"data":{"id":"1","relationships":{"list_items":{"data":[{"id":"7"}]}}}}'],
)
async def test_outage_or_partial_read_makes_lists_unavailable(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data, action: object
) -> None:
    skylight.add_item("1", "Milk")
    await setup(hass, entry_data)
    assert hass.states.get(GROCERY).state == "1"
    skylight.fail("GET", r"/lists/1$", action)
    await poll(hass)
    assert hass.states.get(GROCERY).state == STATE_UNAVAILABLE
    assert hass.states.get(COSTCO).state == STATE_UNAVAILABLE
    await poll(hass, 260)
    assert hass.states.get(GROCERY).state == "1"


async def test_list_deleted_in_skylight_becomes_unavailable(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    skylight.add_item("2", "Paper towels")
    await setup(hass, entry_data)
    del skylight.lists["2"]
    await poll(hass)
    assert hass.states.get(COSTCO).state == STATE_UNAVAILABLE
    assert hass.states.get(GROCERY).state == "0"


async def test_options_set_poll_interval(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    await setup(hass, entry_data, **{CONF_LISTS_INTERVAL: 600})
    before = len(skylight.calls_to("GET", r"/lists$"))
    await poll(hass, 130)
    assert len(skylight.calls_to("GET", r"/lists$")) == before
    await poll(hass, 610)
    assert len(skylight.calls_to("GET", r"/lists$")) == before + 1


# -- authentication -----------------------------------------------------


async def test_token_rotation_is_persisted_without_reload(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    entry = await setup(hass, entry_data)
    calls_before = len(skylight.calls_to("GET", r"/api/frames/\d+/lists$"))
    skylight.access_token = "server-rotated"
    await poll(hass)
    assert entry.data[CONF_ACCESS_TOKEN] == skylight.access_token
    assert entry.data[CONF_REFRESH_TOKEN] == skylight.refresh_token
    assert entry.state is ConfigEntryState.LOADED
    # Exactly one extra list poll (the retried one): no reload re-fetch.
    assert len(skylight.calls_to("GET", r"/api/frames/\d+/lists$")) == calls_before + 2


async def test_rejected_refresh_starts_reauth(hass: HomeAssistant, skylight: FakeSkylight, entry_data) -> None:
    entry = await setup(hass, entry_data)
    skylight.access_token = "server-rotated"
    skylight.refresh_token = "revoked"
    await poll(hass)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == entry.entry_id


async def test_missing_tokens_start_reauth(hass: HomeAssistant, entry_data) -> None:
    entry_data.pop(CONF_REFRESH_TOKEN)
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress()[0]["context"]["source"] == SOURCE_REAUTH


async def test_logs_contain_no_secrets(
    hass: HomeAssistant, skylight: FakeSkylight, entry_data, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    skylight.add_item("1", "Private item")
    await setup(hass, entry_data)
    skylight.access_token = "server-rotated"
    await poll(hass)
    skylight.fail("GET", r"/lists/1$", 500)
    await poll(hass)
    await poll(hass, 260)
    skylight.fail("POST", r"/list_items$", 422)
    with pytest.raises(HomeAssistantError):
        await todo(hass, "add_item", {"item": "Another private item"})
    skylight.access_token = "again"
    skylight.refresh_token = "revoked"
    await poll(hass)
    assert "SECRET" not in caplog.text
    assert skylight.access_token not in caplog.text
    assert "server said" not in caplog.text
