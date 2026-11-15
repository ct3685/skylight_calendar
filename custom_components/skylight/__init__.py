"""Unofficial, deliberately list-only Skylight integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr

from .api import SkylightAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_FINGERPRINT,
    CONF_FRAME_ID,
    CONF_FRAME_NAME,
    CONF_LISTS_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LISTS_SCAN_INTERVAL,
)
from .coordinator import SkylightListsCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["todo"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    token = entry.data.get(CONF_ACCESS_TOKEN)
    refresh = entry.data.get(CONF_REFRESH_TOKEN)
    frame_id = entry.data.get(CONF_FRAME_ID)
    fingerprint = entry.data.get(CONF_DEVICE_FINGERPRINT, "")
    frame_name = entry.data.get(CONF_FRAME_NAME) or f"Skylight Frame {frame_id}"
    if not token or not refresh or not frame_id:
        _LOGGER.error("Skylight list integration missing required configuration")
        return False

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
        hass, api, frame_id,
        int(entry.options.get(CONF_LISTS_INTERVAL, LISTS_SCAN_INTERVAL)),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "frame_id": frame_id,
        "frame_name": frame_name,
        "lists_coordinator": coordinator,
    }
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, frame_id)},
        manufacturer="Skylight",
        name=frame_name,
        model="Calendar Frame",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
