"""Dependency-free static regression checks for the reduced integration."""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "skylight"


class ShoppingScopeTests(unittest.TestCase):
    def read(self, filename: str) -> str:
        return (COMP / filename).read_text(encoding="utf-8")

    def test_files_parse(self):
        for path in COMP.glob("*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"))

    def test_only_todo_platform_is_loaded(self):
        module = ast.parse(self.read("__init__.py"))
        values = {
            target.id: stmt.value
            for stmt in module.body
            if isinstance(stmt, ast.Assign)
            for target in stmt.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(ast.literal_eval(values["PLATFORMS"]), ["todo"])

    def test_api_exposes_only_list_operations(self):
        source = self.read("api.py")
        for method in ("get_lists", "get_list_items", "add_list_item",
                       "update_list_item", "delete_list_item", "get_frames"):
            self.assertIn("async def " + method, source)
        for blocked in ("upload_media", "delete_chore", "delete_calendar_event",
                        "get_messages", "get_reward_points", "patch_device",
                        "get_devices", "get_cloud_upload_credentials"):
            self.assertNotIn("def " + blocked, source)

    def test_no_request_body_or_raw_response_logging(self):
        source = self.read("api.py")
        self.assertNotIn("_LOGGER.", source)
        self.assertNotIn("resp.text()[:", source)
        self.assertNotIn("raise SkylightAPIError(f\"{method} {path}", source)
        self.assertIn("allow_redirects=False", source)

    def test_no_dynamic_code_execution_in_runtime(self):
        for path in COMP.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertFalse(calls.intersection({"eval", "exec", "compile"}),
                             f"dynamic execution in {path.name}")

    def test_oauth_scope_is_explicitly_documented(self):
        self.assertIn('OAUTH_SCOPE = "everything"', self.read("const.py"))
        self.assertIn("scope=everything", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
