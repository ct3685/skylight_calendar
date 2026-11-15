"""Boot a real Home Assistant core with this integration and the fake Skylight.

The pytest harness lags Home Assistant releases; this checks the integration
against whatever ``homeassistant`` version is installed (e.g. 2026.11.1):
config flow sign-in, entity setup, every todo action, outage handling, unload.

    python scripts/ha_core_smoke.py
"""

from __future__ import annotations

import homeassistant  # noqa: F401 - must be imported first (aliases httpx)

import asyncio
import logging
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from homeassistant import bootstrap, runner  # noqa: E402
from homeassistant.const import STATE_UNAVAILABLE, __version__  # noqa: E402

import custom_components.skylight.config_flow  # noqa: E402, F401 - patched below
from tests.conftest import FakeSkylight  # noqa: E402

ENTITY = "todo.test_home_grocery"


async def todo(hass, service: str, data: dict) -> None:
    await hass.services.async_call("todo", service, data, target={"entity_id": ENTITY}, blocking=True)


async def items(hass) -> list[tuple[str, str]]:
    resp = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": ENTITY}, blocking=True, return_response=True
    )
    return [(i["summary"], i["status"]) for i in resp[ENTITY]["items"]]


def prepare_config_dir(config_dir: str) -> None:
    os.symlink(ROOT / "custom_components", pathlib.Path(config_dir) / "custom_components")
    pathlib.Path(config_dir, "configuration.yaml").write_text("homeassistant:\n")


async def main() -> None:
    fake = FakeSkylight()
    fake.add_list("1", "Grocery")
    keep = fake.add_item("1", "Eggs")
    with tempfile.TemporaryDirectory() as config_dir:
        await asyncio.to_thread(prepare_config_dir, config_dir)
        # Patch the integration's own references so nothing reaches the network.
        with (
            patch("custom_components.skylight.async_get_clientsession", return_value=fake),
            patch("custom_components.skylight.config_flow.async_get_clientsession", return_value=fake),
            patch("aiohttp.ClientSession._request", side_effect=AssertionError("real network call")),
        ):
            hass = await bootstrap.async_setup_hass(
                runner.RuntimeConfig(config_dir=config_dir, skip_pip=True, safe_mode=False)
            )
            assert hass is not None, "Home Assistant failed to start"
            try:
                await run(hass, fake, keep)
            finally:
                await hass.async_stop(force=True)
    print(f"OK: Home Assistant {__version__} smoke test passed")


async def run(hass, fake: FakeSkylight, keep: str) -> None:
    flow = await hass.config_entries.flow.async_init("skylight", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"code": "https://ourskylight.com/welcome?code=good-code"}
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("skylight")[0]
    assert entry.state.value == "loaded", entry.state
    assert hass.states.get(ENTITY) is not None

    await todo(hass, "add_item", {"item": "Milk"})
    milk = next(i for i, d in fake.lists["1"]["items"].items() if d["label"] == "Milk")
    await todo(hass, "update_item", {"item": milk, "status": "completed"})
    assert fake.lists["1"]["items"][milk]["status"] == "completed"
    await todo(hass, "update_item", {"item": milk, "status": "needs_action", "rename": "Oat milk"})
    assert fake.lists["1"]["items"][milk] == {"label": "Oat milk", "status": "pending", "position": 1}
    await todo(hass, "remove_item", {"item": [milk]})
    assert list(fake.lists["1"]["items"]) == [keep]
    assert await items(hass) == [("Eggs", "needs_action")]

    fake.fail("GET", r"/lists/1$", 503)
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(ENTITY).state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
