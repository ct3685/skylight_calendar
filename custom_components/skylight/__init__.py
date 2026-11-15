"""Unofficial, deliberately list-only Skylight integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SkylightAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_FINGERPRINT,
    CONF_FRAME_ID,
    CONF_FRAME_NAME,
    CONF_LISTS_INTERVAL,
    CONF_REFRESH_TOKEN,
    LISTS_SCAN_INTERVAL,
)
from .coordinator import SkylightListsCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.TODO]


@dataclass
class SkylightData:
    api: SkylightAPI
    coordinator: SkylightListsCoordinator
    frame_id: str
    frame_name: str
    options: dict


type SkylightConfigEntry = ConfigEntry[SkylightData]


async def async_setup_entry(hass: HomeAssistant, entry: SkylightConfigEntry) -> bool:
    token = entry.data.get(CONF_ACCESS_TOKEN)
    refresh = entry.data.get(CONF_REFRESH_TOKEN)
    frame_id = entry.data.get(CONF_FRAME_ID)
    fingerprint = entry.data.get(CONF_DEVICE_FINGERPRINT, "")
    frame_name = entry.data.get(CONF_FRAME_NAME) or f"Skylight Frame {frame_id}"
    if not frame_id:
        _LOGGER.error("Skylight list integration is missing its frame id")
        return False
    if not token or not refresh:
        raise ConfigEntryAuthFailed("Skylight credentials are missing")

    async def _persist_tokens(
        new_access: str, new_refresh: str, new_fp: str | None
    ) -> None:
        updated = {
            **entry.data,
            CONF_ACCESS_TOKEN: new_access,
            CONF_REFRESH_TOKEN: new_refresh,
        }
        if new_fp:
            updated[CONF_DEVICE_FINGERPRINT] = new_fp
        hass.config_entries.async_update_entry(entry, data=updated)

    api = SkylightAPI(
        session=async_get_clientsession(hass),
        access_token=token,
        refresh_token=refresh,
        device_fingerprint=fingerprint,
        token_update_cb=_persist_tokens,
    )
    coordinator = SkylightListsCoordinator(
        hass,
        entry,
        api,
        frame_id,
        int(entry.options.get(CONF_LISTS_INTERVAL, LISTS_SCAN_INTERVAL)),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SkylightData(
        api=api,
        coordinator=coordinator,
        frame_id=frame_id,
        frame_name=frame_name,
        options=dict(entry.options),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: SkylightConfigEntry) -> None:
    # Token rotation also updates the entry; only an options change needs a reload.
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SkylightConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
