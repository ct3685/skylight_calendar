"""Refresh only Skylight shopping/to-do lists, no calendar or photo access."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SkylightAPI, SkylightAPIError
from .const import DOMAIN, LISTS_SCAN_INTERVAL


class SkylightListsCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        api: SkylightAPI,
        frame_id: str,
        update_interval: int = LISTS_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            __import__("logging").getLogger(__name__),
            name=f"{DOMAIN} lists {frame_id}",
            update_interval=timedelta(seconds=update_interval),
        )
        self.api = api
        self.frame_id = frame_id

    async def _async_update_data(self) -> dict:
        try:
            response = await self.api.get_lists(self.frame_id)
            if not isinstance(response.get("data"), list):
                raise SkylightAPIError("Skylight list feed has an unexpected format")
            result: dict[str, dict] = {}
            for entry in response["data"]:
                list_id = str(entry.get("id") or "")
                if not list_id:
                    continue
                attrs = entry.get("attributes") or {}
                detail = await self.api.get_list_items(self.frame_id, list_id)
                included = detail.get("included", [])
                if not isinstance(included, list):
                    raise SkylightAPIError("Skylight list items have an unexpected format")
                # A missing include on a nonempty list is not an empty list:
                # treating it as empty could cause a sync engine to erase items.
                data = detail.get("data") or {}
                relationships = (data.get("relationships") or {}).get("list_items") or {}
                expected = relationships.get("data")
                if expected and not included:
                    raise SkylightAPIError("Skylight omitted requested list items")
                items = []
                for obj in included:
                    if obj.get("type") != "list_item":
                        continue
                    item_id = str(obj.get("id") or "")
                    item_attrs = obj.get("attributes") or {}
                    if not item_id:
                        continue
                    items.append({
                        "id": item_id,
                        "name": item_attrs.get("label") or item_attrs.get("name") or "",
                        "status": item_attrs.get("status", "pending"),
                        "position": item_attrs.get("position", 0) or 0,
                    })
                items.sort(key=lambda item: item["position"])
                result[list_id] = {
                    "name": attrs.get("label") or f"List {list_id}",
                    "kind": attrs.get("kind"),
                    "items": items,
                }
            return result
        except SkylightAPIError as err:
            raise UpdateFailed(str(err)) from None
