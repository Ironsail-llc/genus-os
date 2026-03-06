"""Tests for the list_directory tool."""

from __future__ import annotations

from pathlib import Path

from robothor.engine.tools import READONLY_TOOLS


class TestListDirectoryReadonly:
    def test_list_directory_in_readonly_tools(self):
        assert "list_directory" in READONLY_TOOLS

    def test_list_directory_is_not_write_tool(self):
        write_tools = {"write_file", "exec", "create_task", "store_memory"}
        for tool in write_tools:
            assert tool not in READONLY_TOOLS or tool == "list_directory"


class TestListDirectorySchema:
    def test_schema_registered(self):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        schemas = {s["function"]["name"]: s for s in registry._schemas.values()}
        assert "list_directory" in schemas

    def test_schema_has_required_path(self):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        schema = registry._schemas["list_directory"]
        params = schema["function"]["parameters"]
        assert "path" in params["properties"]
        assert "path" in params["required"]

    def test_schema_has_optional_pattern(self):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        schema = registry._schemas["list_directory"]
        params = schema["function"]["parameters"]
        assert "pattern" in params["properties"]
        assert "pattern" not in params["required"]

    def test_schema_has_optional_recursive(self):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        schema = registry._schemas["list_directory"]
        params = schema["function"]["parameters"]
        assert "recursive" in params["properties"]
        assert "recursive" not in params["required"]


class TestListDirectoryHandler:
    """Test the list_directory handler via _handle_sync_tool."""

    def _call(self, args: dict, workspace: str | None = None) -> dict:
        from robothor.engine.tools import _handle_sync_tool

        return _handle_sync_tool("list_directory", args, workspace=workspace or "")

    def test_basic_listing(self, tmp_path: Path):
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.md").write_text("world")
        (tmp_path / "subdir").mkdir()

        result = self._call({"path": str(tmp_path)})

        assert "error" not in result
        assert result["count"] == 3
        names = {e["name"] for e in result["entries"]}
        assert "file1.txt" in names
        assert "file2.md" in names
        assert "subdir" in names

    def test_entry_types(self, tmp_path: Path):
        (tmp_path / "file.txt").write_text("content")
        (tmp_path / "subdir").mkdir()

        result = self._call({"path": str(tmp_path)})

        entries_by_name = {e["name"]: e for e in result["entries"]}
        assert entries_by_name["file.txt"]["type"] == "file"
        assert entries_by_name["file.txt"]["size"] > 0
        assert entries_by_name["subdir"]["type"] == "dir"
        assert entries_by_name["subdir"]["size"] == 0

    def test_glob_pattern(self, tmp_path: Path):
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.md").write_text("world")
        (tmp_path / "file3.txt").write_text("!")

        result = self._call({"path": str(tmp_path), "pattern": "*.txt"})

        assert result["count"] == 2
        names = {e["name"] for e in result["entries"]}
        assert "file1.txt" in names
        assert "file3.txt" in names
        assert "file2.md" not in names

    def test_recursive_pattern(self, tmp_path: Path):
        (tmp_path / "top.yaml").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.yaml").write_text("b")
        (sub / "other.txt").write_text("c")

        result = self._call({"path": str(tmp_path), "pattern": "*.yaml", "recursive": True})

        assert result["count"] == 2
        names = {e["name"] for e in result["entries"]}
        assert "top.yaml" in names
        assert str(Path("sub") / "deep.yaml") in names

    def test_relative_path_resolution(self, tmp_path: Path):
        sub = tmp_path / "brain" / "agents"
        sub.mkdir(parents=True)
        (sub / "main.yaml").write_text("id: main")

        result = self._call({"path": "brain/agents"}, workspace=str(tmp_path))

        assert "error" not in result
        assert result["count"] == 1
        assert result["entries"][0]["name"] == "main.yaml"

    def test_nonexistent_path(self, tmp_path: Path):
        result = self._call({"path": str(tmp_path / "nope")})

        assert "error" in result
        assert "does not exist" in result["error"]

    def test_not_a_directory(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("hello")

        result = self._call({"path": str(f)})

        assert "error" in result
        assert "Not a directory" in result["error"]

    def test_truncation_cap(self, tmp_path: Path):
        # Create 210 files to exceed the 200 cap
        for i in range(210):
            (tmp_path / f"file_{i:04d}.txt").write_text(f"content {i}")

        result = self._call({"path": str(tmp_path)})

        assert result["count"] == 200
        assert result["truncated"] is True

    def test_empty_directory(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        result = self._call({"path": str(empty)})

        assert result["count"] == 0
        assert result["entries"] == []
        assert result["truncated"] is False
