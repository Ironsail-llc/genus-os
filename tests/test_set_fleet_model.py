"""Tests for ``scripts/set-fleet-model.py``.

The fleet's fallback chain lives in 20+ hand-written manifests that carry
comments explaining WHY each model was chosen. A yaml.safe_load/dump cycle
deletes those comments, so the tool rewrites the ``fallbacks:`` list
textually — and these tests hold it to that: comments survive, the list
changes, and the two styles the fleet actually uses (inline flow and block
sequence) each stay in their own style.

All fixtures use generic agent ids (agent-a, agent-b); the real manifests are
instance data and are never read by these tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "set-fleet-model.py"


def load_tool(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the script fresh with ``ROBOTHOR_WORKSPACE`` pointed at a tmp dir.

    WORKSPACE is resolved at import time, so the env var has to be set before
    the module body runs — hence a fresh module object per test rather than a
    module-level import.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    spec = importlib.util.spec_from_file_location("set_fleet_model", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


INLINE = """\
id: agent-a
name: Agent A

# ── Model ──────────────────────────────────────────────
model:
  # ox-alpha handles the long-context planning this agent does.
  primary: openrouter/stealth/ox-alpha
  fallbacks: ["ollama_chat/qwen3.8:27b"]  # offline tier
  payload_alias: ox

schedule:
  cron: "0 6 * * *"
"""

BLOCK = """\
id: agent-b
name: Agent B

model:
  primary: openrouter/stealth/ox-alpha
  # The chain, widest first.
  fallbacks:
    - openrouter/xiaomi/mimo-v2.5-pro
  payload_alias: mimo

schedule:
  cron: "0 7 * * *"
"""

CHAIN = "openrouter/xiaomi/mimo-v2.5,openrouter/deepseek/deepseek-v4-flash,ollama_chat/qwen3.8:27b"
CHAIN_LIST = CHAIN.split(",")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def agents_dir(workspace: Path) -> Path:
    d = workspace / "docs" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(mod: ModuleType, *argv: str) -> int:
    import sys

    old = sys.argv
    sys.argv = ["set-fleet-model.py", *argv]
    try:
        return mod.main()
    finally:
        sys.argv = old


class TestFallbackRewrite:
    def test_inline_list_rewritten_and_comments_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        text = path.read_text()
        assert "# ── Model ──" in text
        assert "# ox-alpha handles the long-context planning this agent does." in text
        assert yaml.safe_load(text)["model"]["fallbacks"] == CHAIN_LIST
        # Same textual style: still one inline flow line.
        assert 'fallbacks: ["openrouter/xiaomi/mimo-v2.5", ' in text
        # primary untouched when only --fallbacks is given
        assert yaml.safe_load(text)["model"]["primary"] == "openrouter/stealth/ox-alpha"

    def test_block_list_rewritten_in_block_style(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-b.yaml", BLOCK)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        text = path.read_text()
        assert "# The chain, widest first." in text
        assert "  payload_alias: mimo" in text
        assert yaml.safe_load(text)["model"]["fallbacks"] == CHAIN_LIST
        assert "  fallbacks:\n    - openrouter/xiaomi/mimo-v2.5\n" in text

    def test_every_model_block_in_a_manifest_is_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """heartbeat/worker blocks carry their own chain — the 2026-08-23 hiding place."""
        mod = load_tool(tmp_path, monkeypatch)
        nested = """\
id: agent-a
model:
  primary: openrouter/stealth/ox-alpha
  fallbacks: ["ollama_chat/qwen3.8:27b"]
heartbeat:
  model:
    primary: openrouter/stealth/ox-alpha
    fallbacks: ["ollama_chat/qwen3.8:27b"]
"""
        path = write(agents_dir(tmp_path) / "agent-a.yaml", nested)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        data = yaml.safe_load(path.read_text())
        assert data["model"]["fallbacks"] == CHAIN_LIST
        assert data["heartbeat"]["model"]["fallbacks"] == CHAIN_LIST

    def test_empty_inline_list_is_filled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(
            agents_dir(tmp_path) / "agent-a.yaml",
            "id: agent-a\nmodel:\n  primary: openrouter/stealth/ox-alpha\n  fallbacks: []\n",
        )

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        assert yaml.safe_load(path.read_text())["model"]["fallbacks"] == CHAIN_LIST


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN) == 0

        assert path.read_text() == INLINE

    def test_dry_run_is_the_default_for_primary_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", INLINE)

        assert (
            run(
                mod,
                "--from",
                "openrouter/stealth/ox-alpha",
                "--primary",
                "openrouter/xiaomi/mimo-v2.5",
            )
            == 0
        )

        assert path.read_text() == INLINE


class TestScope:
    def test_retired_and_archived_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        base = agents_dir(tmp_path)
        retired = write(base / "retired" / "agent-a.yaml", INLINE)
        archived = write(base / ".archived" / "agent-b.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        assert retired.read_text() == INLINE
        assert archived.read_text() == INLINE

    def test_delphi_subdir_is_included(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "delphi" / "agent-b.yaml", BLOCK)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        assert yaml.safe_load(path.read_text())["model"]["fallbacks"] == CHAIN_LIST

    def test_defaults_skipped_without_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "_defaults.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        assert path.read_text() == INLINE

    def test_defaults_rewritten_with_include_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "_defaults.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN, "--include-defaults", "--apply") == 0

        assert yaml.safe_load(path.read_text())["model"]["fallbacks"] == CHAIN_LIST

    def test_schema_yaml_is_never_touched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "schema.yaml", INLINE)

        assert run(mod, "--fallbacks", CHAIN, "--include-defaults", "--apply") == 0

        assert path.read_text() == INLINE


class TestPrimaryStillWorks:
    def test_primary_swap_honours_from_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        base = agents_dir(tmp_path)
        on_old = write(base / "agent-a.yaml", INLINE)
        on_other = write(
            base / "agent-b.yaml",
            "id: agent-b\nmodel:\n  primary: ollama_chat/qwen3:8b\n  fallbacks: []\n",
        )

        assert (
            run(
                mod,
                "--from",
                "openrouter/stealth/ox-alpha",
                "--primary",
                "openrouter/xiaomi/mimo-v2.5",
                "--apply",
            )
            == 0
        )

        assert yaml.safe_load(on_old.read_text())["model"]["primary"] == (
            "openrouter/xiaomi/mimo-v2.5"
        )
        assert yaml.safe_load(on_other.read_text())["model"]["primary"] == "ollama_chat/qwen3:8b"

    def test_primary_and_fallbacks_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", INLINE)

        assert (
            run(
                mod,
                "--from",
                "openrouter/stealth/ox-alpha",
                "--primary",
                "openrouter/xiaomi/mimo-v2.5",
                "--fallbacks",
                CHAIN,
                "--apply",
            )
            == 0
        )

        data = yaml.safe_load(path.read_text())
        assert data["model"]["primary"] == "openrouter/xiaomi/mimo-v2.5"
        assert data["model"]["fallbacks"] == CHAIN_LIST


class TestArgumentContract:
    def test_neither_primary_nor_fallbacks_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            run(mod, "--apply")
        assert exc.value.code != 0

    def test_primary_without_from_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--from is what keeps a fleet swap off deliberately-different agents."""
        mod = load_tool(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            run(mod, "--primary", "openrouter/xiaomi/mimo-v2.5", "--apply")
        assert exc.value.code != 0

    def test_manifest_without_a_fallbacks_line_is_reported_not_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """A manifest the chain did NOT reach must not look like it was applied."""
        mod = load_tool(tmp_path, monkeypatch)
        write(
            agents_dir(tmp_path) / "agent-a.yaml",
            "id: agent-a\nmodel:\n  primary: openrouter/stealth/ox-alpha\n",
        )

        # Unapplied under --apply is a FAILURE, not a line of scrollback.
        assert run(mod, "--fallbacks", CHAIN, "--apply") == 1

        out = capsys.readouterr().out
        assert "agent-a.yaml" in out
        assert "no fallbacks" in out.lower()

    def test_a_style_the_tool_cannot_rewrite_is_reported_not_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """An anchor/alias is left alone — but the operator has to be told."""
        mod = load_tool(tmp_path, monkeypatch)
        path = write(
            agents_dir(tmp_path) / "agent-a.yaml",
            "id: agent-a\nchain: &chain\n  - ollama_chat/qwen3.8:27b\n"
            "model:\n  primary: openrouter/stealth/ox-alpha\n  fallbacks: *chain\n",
        )
        original = path.read_text()

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 1

        assert path.read_text() == original
        assert "not applied" in capsys.readouterr().out.lower()

    def test_empty_chain_clears_a_block_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """An empty block sequence is not valid YAML for a list — collapse to []."""
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-b.yaml", BLOCK)

        assert run(mod, "--fallbacks", "", "--apply") == 0

        assert yaml.safe_load(path.read_text())["model"]["fallbacks"] == []


PROSE = """\
id: agent-a
name: Agent A

instructions: |
  When you choose a model, write the chain out in full, e.g.
  fallbacks: ["some/example"]
  and never guess at it.
"""

PROSE_PLUS_KEY = """\
id: agent-a
name: Agent A

instructions: |
  When you choose a model, write the chain out in full, e.g.
  fallbacks: ["some/example"]
  and never guess at it.

model:
  primary: openrouter/stealth/ox-alpha
  fallbacks: ["ollama_chat/qwen3.8:27b"]  # offline tier
"""


class TestProseIsNotAMappingKey:
    """A ``fallbacks:`` line inside an ``instructions: |`` block is PROSE.

    Rewriting it silently edits an agent's instructions — the one kind of
    damage a model swap must never do — and the old verifier could not see it
    because it only walked mapping keys.
    """

    def test_fallbacks_inside_a_block_scalar_is_left_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", PROSE)

        run(mod, "--fallbacks", CHAIN, "--apply")

        assert path.read_text() == PROSE
        out = capsys.readouterr().out
        assert "agent-a.yaml" in out
        assert "SKIPPED" in out

    def test_prose_does_not_stop_a_real_key_being_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-a.yaml", PROSE_PLUS_KEY)

        run(mod, "--fallbacks", CHAIN, "--apply")

        data = yaml.safe_load(path.read_text())
        assert data["model"]["fallbacks"] == CHAIN_LIST
        # The prose is untouched, comment and all.
        assert 'fallbacks: ["some/example"]' in data["instructions"]


class TestVerifier:
    """The verifier proves the rewrite changed the fallbacks keys and NOTHING else."""

    def test_a_clean_rewrite_verifies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        mod = load_tool(tmp_path, monkeypatch)
        original = 'id: agent-a\nmodel:\n  fallbacks: ["ollama_chat/qwen3.8:27b"]\n'
        updated = f"id: agent-a\nmodel:\n  fallbacks: {CHAIN_LIST!r}\n"

        assert mod._verify(original, updated, CHAIN_LIST) is None

    def test_a_change_outside_a_fallbacks_key_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        original = 'id: agent-a\nmodel:\n  fallbacks: ["ollama_chat/qwen3.8:27b"]\n'
        tampered = f"id: agent-b\nmodel:\n  fallbacks: {CHAIN_LIST!r}\n"

        assert mod._verify(original, tampered, CHAIN_LIST) == (
            "rewrite touched something other than a fallbacks key"
        )


class TestExitStatus:
    """A chain that missed a manifest must not exit 0.

    The 2026-08-22 swap "succeeded" seven times while manifests kept the old
    model. Anything scripted around this tool reads the exit code, so an
    unreached manifest has to be a failure, not a line of scrollback.
    """

    NO_KEY = "id: agent-c\nmodel:\n  primary: openrouter/stealth/ox-alpha\n"

    def test_apply_returns_one_when_a_manifest_did_not_get_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        mod = load_tool(tmp_path, monkeypatch)
        base = agents_dir(tmp_path)
        write(base / "agent-a.yaml", INLINE)
        write(base / "agent-c.yaml", self.NO_KEY)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 1

        out = capsys.readouterr().out
        assert "did NOT get the chain" in out
        assert "docs/agents/agent-c.yaml" in out.split("did NOT get the chain")[1]

    def test_a_skipped_manifest_counts_as_unapplied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        write(agents_dir(tmp_path) / "agent-a.yaml", PROSE)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 1

    def test_dry_run_returns_zero_but_prints_the_same_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        mod = load_tool(tmp_path, monkeypatch)
        write(agents_dir(tmp_path) / "agent-c.yaml", self.NO_KEY)

        assert run(mod, "--fallbacks", CHAIN) == 0

        out = capsys.readouterr().out
        assert "did NOT get the chain" in out
        assert "docs/agents/agent-c.yaml" in out.split("did NOT get the chain")[1]

    def test_apply_returns_zero_when_every_manifest_took_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        mod = load_tool(tmp_path, monkeypatch)
        base = agents_dir(tmp_path)
        write(base / "agent-a.yaml", INLINE)
        write(base / "agent-b.yaml", BLOCK)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        assert "did NOT get the chain" not in capsys.readouterr().out


BLOCK_WITH_COMMENT = """\
id: agent-b
name: Agent B

model:
  primary: openrouter/stealth/ox-alpha
  fallbacks:
    - openrouter/xiaomi/mimo-v2.5-pro
    # offline tier — the box keeps answering when the API is capped
    - ollama_chat/qwen3.8:27b
  payload_alias: mimo
"""


class TestInterleavedComments:
    """A comment BETWEEN block items used to orphan every item after it.

    ``_BLOCK_ITEM`` did not match the comment line, so the loop stopped there
    and the remaining ``- model`` lines were copied out below the new chain —
    still part of the same YAML sequence. The list came out wrong, and the
    only report was the verifier's misleading "fallbacks did not take".
    """

    def test_the_chain_replaces_every_item_and_the_comment_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        path = write(agents_dir(tmp_path) / "agent-b.yaml", BLOCK_WITH_COMMENT)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        text = path.read_text()
        assert yaml.safe_load(text)["model"]["fallbacks"] == CHAIN_LIST
        assert "# offline tier — the box keeps answering when the API is capped" in text
        assert "  payload_alias: mimo" in text
        # The old items are gone, not pushed below the new ones.
        assert "mimo-v2.5-pro" not in text

    def test_a_trailing_comment_after_the_last_item_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mod = load_tool(tmp_path, monkeypatch)
        source = (
            "id: agent-b\nmodel:\n  fallbacks:\n    - openrouter/xiaomi/mimo-v2.5-pro\n"
            "    # keep this chain short\n  payload_alias: mimo\n"
        )
        path = write(agents_dir(tmp_path) / "agent-b.yaml", source)

        assert run(mod, "--fallbacks", CHAIN, "--apply") == 0

        text = path.read_text()
        assert yaml.safe_load(text)["model"]["fallbacks"] == CHAIN_LIST
        assert "    # keep this chain short\n  payload_alias: mimo\n" in text
