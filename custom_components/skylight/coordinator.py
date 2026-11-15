"""Refresh only Skylight shopping/to-do lists, no calendar or photo access.

A sync engine reads these lists and mirrors absences as deletions, so a
response that may be incomplete must fail the whole refresh (entities go
unavailable) rather than look like a shorter list.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SkylightAPI, SkylightAPIError, SkylightAuthError
from .const import DOMAIN, ITEM_STATUS_PENDING, LISTS_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

SkylightLists = dict[str, dict[str, Any]]


def _position(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_item(obj: dict[str, Any]) -> dict[str, Any]:
    item_id = obj.get("id")
    if item_id is None or str(item_id) == "":
        raise SkylightAPIError("Skylight returned a list item without an id")
    attrs = obj.get("attributes")
    if not isinstance(attrs, dict):
        raise SkylightAPIError("Skylight returned a list item without attributes")
    label = attrs.get("label")
    if not isinstance(label, str):
        label = attrs.get("name") if isinstance(attrs.get("name"), str) else ""
    status = attrs.get("status")
    return {
        "id": str(item_id),
        "name": label,
        "status": status if isinstance(status, str) else ITEM_STATUS_PENDING,
        "position": _position(attrs.get("position")),
    }


def _collect(records: list[Any]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for obj in records:
        if not isinstance(obj, dict) or obj.get("type") != "list_item":
            continue
        item = _parse_item(obj)
        items.setdefault(item["id"], item)
    return sorted(items.values(), key=lambda item: item["position"])


def _has_next_page(doc: dict[str, Any]) -> bool:
    links = doc.get("links")
    return isinstance(links, dict) and bool(links.get("next"))


def parse_list_detail(detail: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Items of ``GET /lists/{id}?include=list_items``.

    Returns None when the response cannot tell an empty list from a list
    whose items were left out; raises when it is visibly incomplete.
    """
    data = detail.get("data")
    if not isinstance(data, dict):
        raise SkylightAPIError("Skylight list response has no list record")
    if _has_next_page(detail):
        raise SkylightAPIError("Skylight paginated a list; refusing a partial read")
    included = detail.get("included")
    if included is None:
        included = []
    if not isinstance(included, list):
        raise SkylightAPIError("Skylight list items have an unexpected format")

    relationship = ((data.get("relationships") or {}).get("list_items") or {}).get(
        "data"
    )
    items = _collect(included)
    if isinstance(relationship, list):
        expected = {
            str(ref["id"])
            for ref in relationship
            if isinstance(ref, dict) and ref.get("id") is not None
        }
        missing = expected - {item["id"] for item in items}
        if missing:
            raise SkylightAPIError("Skylight omitted some requested list items")
        return items
    if items:
        return items
    return None


def parse_item_collection(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Items of ``GET /lists/{id}/list_items``."""
    records = doc.get("data")
    if not isinstance(records, list):
        raise SkylightAPIError("Skylight list item collection has an unexpected format")
    if _has_next_page(doc):
        raise SkylightAPIError("Skylight paginated a list; refusing a partial read")
    return _collect(records)


async def fetch_list_items(
    api: SkylightAPI, frame_id: str, list_id: str
) -> list[dict[str, Any]]:
    items = parse_list_detail(await api.get_list_items(frame_id, list_id))
    if items is None:
        items = parse_item_collection(
            await api.get_list_item_collection(frame_id, list_id)
        )
    return items


class SkylightListsCoordinator(DataUpdateCoordinator[SkylightLists]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: SkylightAPI,
        frame_id: str,
        update_interval: int = LISTS_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} lists {frame_id}",
            update_interval=timedelta(seconds=update_interval),
        )
        self.api = api
        self.frame_id = frame_id

    async def _async_update_data(self) -> SkylightLists:
        try:
            response = await self.api.get_lists(self.frame_id)
            records = response.get("data")
            if not isinstance(records, list):
                raise SkylightAPIError("Skylight list feed has an unexpected format")
            result: SkylightLists = {}
            for entry in records:
                if not isinstance(entry, dict):
                    continue
                list_id = str(entry.get("id") or "")
                if not list_id or list_id in result:
                    continue
                attrs = entry.get("attributes") or {}
                result[list_id] = {
                    "name": attrs.get("label") or f"List {list_id}",
                    "kind": attrs.get("kind"),
                    "items": await fetch_list_items(self.api, self.frame_id, list_id),
                }
            return result
        except SkylightAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from None
        except SkylightAPIError as err:
            raise UpdateFailed(str(err)) from None
