"""Fail-closed parsing of list reads: incomplete data must never look empty."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.skylight.api import SkylightAPIError
from custom_components.skylight.coordinator import (
    parse_item_collection,
    parse_list_detail,
)


def item(item_id: Any, label: str = "Milk", **attrs: Any) -> dict[str, Any]:
    return {"id": item_id, "type": "list_item", "attributes": {"label": label, **attrs}}


def detail(refs: list[str] | None, included: Any = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": "1", "type": "list"}
    if refs is not None:
        data["relationships"] = {"list_items": {"data": [{"id": r, "type": "list_item"} for r in refs]}}
    doc: dict[str, Any] = {"data": data, **extra}
    if included is not None:
        doc["included"] = included
    return doc


def test_explicitly_empty_list() -> None:
    assert parse_list_detail(detail([])) == []


def test_ambiguous_empty_list_needs_confirmation() -> None:
    assert parse_list_detail(detail(None)) is None


def test_partial_include_fails_closed() -> None:
    with pytest.raises(SkylightAPIError, match="omitted"):
        parse_list_detail(detail(["1", "2"], [item("1")]))


def test_missing_include_on_nonempty_list_fails_closed() -> None:
    with pytest.raises(SkylightAPIError, match="omitted"):
        parse_list_detail(detail(["1"]))


def test_pagination_fails_closed() -> None:
    with pytest.raises(SkylightAPIError, match="paginated"):
        parse_list_detail(detail(["1"], [item("1")], links={"next": "/page/2"}))
    with pytest.raises(SkylightAPIError, match="paginated"):
        parse_item_collection({"data": [item("1")], "links": {"next": "x"}})


@pytest.mark.parametrize("bad_id", [None, ""])
def test_item_without_id_fails_closed(bad_id: Any) -> None:
    with pytest.raises(SkylightAPIError, match="without an id"):
        parse_list_detail(detail([], [item(bad_id)]))


def test_item_without_attributes_fails_closed() -> None:
    with pytest.raises(SkylightAPIError, match="attributes"):
        parse_list_detail(detail(["1"], [{"id": "1", "type": "list_item"}]))


@pytest.mark.parametrize("doc", [{}, {"data": []}, {"data": "x"}, {"data": {}, "included": {}}])
def test_malformed_documents_fail_closed(doc: dict[str, Any]) -> None:
    with pytest.raises(SkylightAPIError):
        parse_list_detail(doc)


def test_collection_requires_data_list() -> None:
    with pytest.raises(SkylightAPIError):
        parse_item_collection({})
    assert parse_item_collection({"data": []}) == []


def test_duplicate_names_are_distinct_items_and_duplicate_records_collapse() -> None:
    items = parse_list_detail(
        detail(["1", "2"], [item("1", "Milk"), item("2", "Milk"), item("1", "Milk")])
    )
    assert [i["id"] for i in items] == ["1", "2"]


def test_other_included_types_are_ignored_and_positions_tolerated() -> None:
    items = parse_list_detail(
        detail(
            ["1", "2"],
            [
                {"id": "9", "type": "section", "attributes": {}},
                item("2", "B", position="x"),
                item("1", "A", position=None, status="completed"),
            ],
        )
    )
    assert [(i["name"], i["status"]) for i in items] == [("B", "pending"), ("A", "completed")]


def test_legacy_name_field_and_missing_label() -> None:
    items = parse_list_detail(
        detail(["1", "2"], [
            {"id": "1", "type": "list_item", "attributes": {"name": "Legacy"}},
            {"id": "2", "type": "list_item", "attributes": {"label": None}},
        ])
    )
    assert [i["name"] for i in items] == ["Legacy", ""]
