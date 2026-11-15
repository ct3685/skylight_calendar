"""Scope and hygiene guards for the reduced, list-only integration."""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re

from custom_components.skylight import PLATFORMS
from custom_components.skylight.api import SkylightAPI

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "skylight"
SOURCES = sorted(COMP.glob("*.py"))


def test_only_todo_platform_is_loaded() -> None:
    assert PLATFORMS == ["todo"]
    assert not (COMP / "services.yaml").exists()
    assert {p.stem for p in SOURCES} == {
        "__init__", "api", "config_flow", "const", "coordinator", "todo",
    }


def test_api_exposes_only_list_operations() -> None:
    public = {
        name for name, _ in inspect.getmembers(SkylightAPI, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {
        "get_frames", "get_lists", "get_list_items", "get_list_item_collection",
        "add_list_item", "update_list_item", "delete_list_item", "bulk_delete_list_items",
    }


def test_only_skylight_hosts_are_referenced() -> None:
    hosts = set()
    for path in SOURCES:
        hosts |= set(re.findall(r"https://([A-Za-z0-9.-]+)", path.read_text(encoding="utf-8")))
    assert hosts <= {"app.ourskylight.com", "ourskylight.com"}


def test_api_module_does_not_log() -> None:
    source = (COMP / "api.py").read_text(encoding="utf-8")
    assert "_LOGGER" not in source and "logging" not in source
    assert source.count("allow_redirects=False") == 2


def test_no_dynamic_execution_shell_or_file_access() -> None:
    banned_calls = {"eval", "exec", "compile", "open", "__import__"}
    banned_modules = {"subprocess", "os", "shutil", "pickle", "pathlib"}
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, f"{node.func.id} in {path.name}"
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned_modules, path.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned_modules, path.name


def test_manifest_has_no_third_party_requirements() -> None:
    manifest = json.loads((COMP / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requirements"] == []
    assert manifest["domain"] == "skylight"


def test_translations_match_and_render_newlines() -> None:
    strings = (COMP / "strings.json").read_text(encoding="utf-8")
    assert strings == (COMP / "translations" / "en.json").read_text(encoding="utf-8")
    description = json.loads(strings)["config"]["step"]["user"]["description"]
    assert "\n2." in description and "\\n" not in description


def test_oauth_scope_is_explicitly_documented() -> None:
    assert 'OAUTH_SCOPE = "everything"' in (COMP / "const.py").read_text(encoding="utf-8")
    assert "scope=everything" in (ROOT / "README.md").read_text(encoding="utf-8")
