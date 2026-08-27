"""An agent is told nothing about the files it was pointed at.

Measured on WildClawBench task_3 (jigsaw): the workspace holds **16 files,
all images, across 4 directories**, and the agent starts knowing none of
it. It must issue directory listings, then a `view_image` per picture,
before any reasoning begins. OpenClaw scores **1.0** on that task.

The comment beside `view_image` in `bench/wildclaw/agent.yaml` already
names the difference:

    the competing harness reads images into context by default, which is
    how it scored where we scored zero on the same multimodal model

The same shape shows up for skills: our agent "could see that a skill
existed, could not read it, and spent its run reverse-engineering the
endpoints — 11 directory listings and 7 file reads on a task it then
failed."

This does not auto-attach images; that would be a different and much
larger change. It tells the agent what is there, so the discovery turns are
spent reasoning instead of groping. Off unless a manifest asks for it,
because an operator's workspace is not a benchmark fixture and a listing of
it is neither useful nor small.
"""

from __future__ import annotations

from robothor.engine.workspace_inventory import workspace_inventory


class TestItDescribesWhatIsThere:
    def test_files_are_listed(self, tmp_path):
        (tmp_path / "notes.md").write_text("x")
        (tmp_path / "data.csv").write_text("a,b")
        text = workspace_inventory(tmp_path)
        assert "notes.md" in text and "data.csv" in text

    def test_images_are_called_out(self, tmp_path):
        (tmp_path / "piece_01.png").write_bytes(b"\x89PNG")
        text = workspace_inventory(tmp_path)
        assert "piece_01.png" in text
        assert "image" in text.lower(), "an agent cannot tell it should look at these"

    def test_subdirectories_are_included(self, tmp_path):
        (tmp_path / "pieces").mkdir()
        (tmp_path / "pieces" / "a.png").write_bytes(b"\x89PNG")
        text = workspace_inventory(tmp_path)
        assert "pieces/a.png" in text

    def test_an_empty_workspace_says_nothing(self, tmp_path):
        assert workspace_inventory(tmp_path) == ""

    def test_a_missing_workspace_says_nothing(self, tmp_path):
        assert workspace_inventory(tmp_path / "absent") == ""


class TestItStaysSmall:
    def test_a_large_workspace_is_capped(self, tmp_path):
        for i in range(500):
            (tmp_path / f"f{i:03}.txt").write_text("x")
        text = workspace_inventory(tmp_path, limit=50)
        assert text.count("\n") < 70, "an inventory that floods context is worse than none"
        assert "500" in text or "more" in text.lower(), "truncation must be visible"

    def test_it_never_raises_on_an_unreadable_path(self, tmp_path):
        assert workspace_inventory("/proc/1/root/nope") == ""


class TestOptIn:
    def test_the_hook_is_silent_without_the_manifest_flag(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.workspace_inventory import inventory_context_hook

        cfg = AgentConfig(id="a", name="A")
        assert inventory_context_hook(cfg) is None

    def test_the_hook_reports_when_asked(self, tmp_path):
        from robothor.engine.models import AgentConfig
        from robothor.engine.workspace_inventory import inventory_context_hook

        (tmp_path / "piece.png").write_bytes(b"\x89PNG")
        cfg = AgentConfig(id="a", name="A")
        cfg.workspace = str(tmp_path)
        cfg.v2 = {"workspace_inventory": True}
        out = inventory_context_hook(cfg)
        assert out and "piece.png" in out


class TestItIsRegistered:
    def test_the_hook_is_wired_into_warmup(self):
        import robothor.engine.workspace_inventory as wi
        from robothor.engine import warmup

        wi.register()
        assert any(
            getattr(h, "__name__", "") == "inventory_context_hook"
            for h in warmup._AGENT_CONTEXT_HOOKS
        ), "the inventory hook is never registered, so warmup never runs it"

    def test_the_daemon_registers_it_at_startup(self):
        """A hook nothing registers is a module, not a feature."""
        import inspect

        from robothor.engine import daemon

        src = inspect.getsource(daemon)
        assert "workspace_inventory" in src, (
            "nothing registers the inventory hook, so warmup never runs it"
        )
