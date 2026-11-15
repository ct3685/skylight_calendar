"""Expose each Skylight list as a Home Assistant to-do entity."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SkylightAPI
from .const import DOMAIN
from .coordinator import SkylightListsCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SkylightListsCoordinator = data["lists_coordinator"]
    api: SkylightAPI = data["api"]
    frame_id: str = data["frame_id"]
    frame_name: str = data["frame_name"]
    known: set[str] = set()

    def _add_new_lists() -> None:
        created = []
        for list_id, list_data in (coordinator.data or {}).items():
            if list_id in known:
                continue
            known.add(list_id)
            created.append(
                SkylightTodoList(
                    coordinator, api, frame_id, frame_name,
                    list_id, list_data["name"]
                )
            )
        if created:
            async_add_entities(created)

    _add_new_lists()

    @callback
    def _on_refresh() -> None:
        _add_new_lists()

    entry.async_on_unload(coordinator.async_add_listener(_on_refresh))


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

    @property
    def todo_items(self) -> list[TodoItem] | None:
        data = (self.coordinator.data or {}).get(self._list_id)
        if data is None:
            return None
        return [
            TodoItem(
                summary=item["name"],
                uid=item["id"],
                status=(
                    TodoItemStatus.COMPLETED
                    if item["status"] == "completed"
                    else TodoItemStatus.NEEDS_ACTION
                ),
            )
            for item in data["items"]
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._api.add_list_item(
            self._frame_id, self._list_id, item.summary or ""
        )
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if not item.uid:
            return
        attrs = {}
        if item.summary is not None:
            # The Skylight list-item API uses "label" in both reads and creates.
            # Rename updates need live testing against a disposable list.
            attrs["label"] = item.summary
        if item.status is not None:
            attrs["status"] = (
                "completed"
                if item.status == TodoItemStatus.COMPLETED
                else "pending"
            )
        if attrs:
            await self._api.update_list_item(
                self._frame_id, self._list_id, item.uid, attrs
            )
            await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self._api.delete_list_item(
                self._frame_id, self._list_id, uid
            )
        await self.coordinator.async_request_refresh()
