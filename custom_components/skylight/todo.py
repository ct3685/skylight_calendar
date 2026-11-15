"""Expose each Skylight list as a Home Assistant to-do entity."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SkylightConfigEntry
from .api import SkylightAPI, SkylightAPIError
from .const import DOMAIN, ITEM_STATUS_COMPLETED, ITEM_STATUS_PENDING
from .coordinator import SkylightListsCoordinator, fetch_list_items, parse_item


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SkylightConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = entry.runtime_data
    coordinator = data.coordinator
    known: set[str] = set()

    @callback
    def _add_new_lists() -> None:
        created = []
        for list_id, list_data in (coordinator.data or {}).items():
            if list_id in known:
                continue
            known.add(list_id)
            created.append(
                SkylightTodoList(
                    coordinator,
                    data.api,
                    data.frame_id,
                    data.frame_name,
                    list_id,
                    list_data["name"],
                )
            )
        if created:
            async_add_entities(created)

    _add_new_lists()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_lists))


def _skylight_status(status: TodoItemStatus) -> str:
    if status == TodoItemStatus.COMPLETED:
        return ITEM_STATUS_COMPLETED
    return ITEM_STATUS_PENDING


class SkylightTodoList(CoordinatorEntity[SkylightListsCoordinator], TodoListEntity):
    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(
        self,
        coordinator: SkylightListsCoordinator,
        api: SkylightAPI,
        frame_id: str,
        frame_name: str,
        list_id: str,
        list_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._frame_id = frame_id
        self._list_id = list_id
        self._attr_name = list_name
        self._attr_unique_id = f"skylight_{frame_id}_list_{list_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, frame_id)},
            name=frame_name,
            manufacturer="Skylight",
            model="Calendar Frame",
        )

    def _list_data(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._list_id)

    @property
    def available(self) -> bool:
        # A list deleted in Skylight must not look like an empty list.
        return super().available and self._list_data() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._list_data() or {}
        return {"skylight_list_id": self._list_id, "skylight_kind": data.get("kind")}

    @property
    def todo_items(self) -> list[TodoItem] | None:
        data = self._list_data()
        if data is None:
            return None
        return [
            TodoItem(
                summary=item["name"],
                uid=item["id"],
                status=(
                    TodoItemStatus.COMPLETED
                    if item["status"] == ITEM_STATUS_COMPLETED
                    else TodoItemStatus.NEEDS_ACTION
                ),
            )
            for item in data["items"]
        ]

    def _current_item(self, uid: str) -> dict[str, Any] | None:
        for item in (self._list_data() or {}).get("items", []):
            if item["id"] == uid:
                return item
        return None

    async def _call(self, func: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except SkylightAPIError as err:
            raise HomeAssistantError(str(err)) from None

    async def async_create_todo_item(self, item: TodoItem) -> None:
        label = (item.summary or "").strip()
        if not label:
            raise ServiceValidationError("A Skylight list item needs a name")
        response = await self._call(
            self._api.add_list_item, self._frame_id, self._list_id, label
        )
        record = response.get("data") if isinstance(response, dict) else None
        try:
            created = parse_item(record) if isinstance(record, dict) else None
        except SkylightAPIError:
            created = None
        if created is not None:
            items = (self._list_data() or {}).get("items", [])
            if all(existing["id"] != created["id"] for existing in items):
                self._set_items([*items, created])
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if not item.uid:
            raise ServiceValidationError("The Skylight list item has no id")
        current = self._current_item(item.uid)
        label: str | None = None
        status: str | None = None
        if item.summary is not None:
            label = item.summary.strip()
            if not label:
                raise ServiceValidationError("A Skylight list item needs a name")
            if current is not None and current["name"] == label:
                label = None
        if item.status is not None:
            status = _skylight_status(item.status)
            if current is not None and current["status"] == status:
                status = None
        if label is None and status is None:
            return
        await self._call(
            self._api.update_list_item,
            self._frame_id,
            self._list_id,
            item.uid,
            label=label,
            status=status,
        )
        # Writes inside the refresh cooldown compare against this copy, so it
        # must reflect the accepted change before the refresh lands.
        changes = {"name": label, "status": status}
        self._set_items(
            [
                {**it, **{k: v for k, v in changes.items() if v is not None}}
                if it["id"] == item.uid
                else it
                for it in (self._list_data() or {}).get("items", [])
            ]
        )
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        uids = list(dict.fromkeys(uids))
        verified: list[dict[str, Any]] | None = None
        try:
            for uid in uids:
                await self._call(
                    self._api.delete_list_item, self._frame_id, self._list_id, uid
                )
            items = await self._verified_items()
            remaining = _present(items, uids)
            if remaining:
                # Some clients report single-item DELETE as accepted but ignored;
                # the web app's bulk endpoint is the fallback.
                await self._call(
                    self._api.bulk_delete_list_items,
                    self._frame_id,
                    self._list_id,
                    remaining,
                )
                items = await self._verified_items()
                remaining = _present(items, uids)
            verified = items
            if remaining:
                raise HomeAssistantError(
                    f"Skylight did not delete {len(remaining)} list item(s)"
                )
        finally:
            if verified is None or not self._set_items(verified):
                await self.coordinator.async_request_refresh()

    async def _verified_items(self) -> list[dict[str, Any]]:
        """A fresh read of this list; raises, because an unverified delete
        must never count as done."""
        try:
            return await fetch_list_items(self._api, self._frame_id, self._list_id)
        except SkylightAPIError as err:
            raise HomeAssistantError(
                f"Could not confirm the Skylight deletion: {err}"
            ) from None

    def _set_items(self, items: list[dict[str, Any]]) -> bool:
        """Show this list's known state now instead of after the refresh cooldown."""
        data = self.coordinator.data
        if not self.coordinator.last_update_success or not data or self._list_id not in data:
            return False
        self.coordinator.async_set_updated_data(
            {**data, self._list_id: {**data[self._list_id], "items": items}}
        )
        return True


def _present(items: list[dict[str, Any]], uids: list[str]) -> list[str]:
    ids = {item["id"] for item in items}
    return [uid for uid in uids if uid in ids]
