"""Read-only markdown projection: inspectability without giving up retrieval.

The Obsidian research returned a narrower answer than the question implied —
markdown wins on human inspectability and loses on retrieval (no vectors, no
ranking, no tenancy, linear scan over 152k facts). So: keep Postgres, steal the
inspectability, and build the projection to be deleted if unread.
"""

from __future__ import annotations

import pytest

from robothor.memory.projection import (
    GENERATED_MARKER,
    projection_enabled,
    project,
    projection_usage_report,
    render_note,
    slugify,
)


class TestSlugify:
    def test_basic(self):
        assert slugify("Alice manages Helios!") == "alice-manages-helios"

    def test_empty_is_not_an_empty_filename(self):
        assert slugify("") == "untitled"

    def test_punctuation_only_is_not_an_empty_filename(self):
        assert slugify("!!! ??? ...") == "untitled"

    def test_is_length_capped(self):
        assert len(slugify("x " * 200)) <= 60

    def test_no_path_traversal_survives(self):
        # A fact containing ../ must not escape the vault directory.
        assert "/" not in slugify("../../etc/passwd")
        assert ".." not in slugify("../../etc/passwd")


class TestRenderNote:
    FACT = {
        "id": 42,
        "fact_text": "Alice manages the Helios project",
        "category": "project",
        "entities": ["Alice", "Helios"],
        "confidence": 0.9,
        "importance_score": 0.8,
        "source_type": "conversation",
        "updated_at": None,
    }

    def test_carries_provenance(self):
        # Without provenance the projection is just assertions in a nicer font.
        note = render_note(self.FACT)
        assert "fact_id: 42" in note
        assert "source_type: conversation" in note
        assert "generated_at:" in note

    def test_is_marked_read_only_and_says_why(self):
        note = render_note(self.FACT)
        assert "read_only: true" in note
        assert "system of record" in note

    def test_carries_the_generator_marker(self):
        # The sweeper deletes only files it made; an operator's own notes in
        # the same vault folder must survive.
        assert GENERATED_MARKER in render_note(self.FACT)

    def test_entities_become_wikilinks(self):
        note = render_note(self.FACT)
        assert "[[Alice]]" in note and "[[Helios]]" in note

    def test_no_entities_is_not_a_crash(self):
        assert render_note({**self.FACT, "entities": None})


class TestFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("MEMORY_PROJECTION", raising=False)
        assert projection_enabled() is False


@pytest.fixture
def a_projectable_fact():
    """Seed one fact worth projecting.

    The projection reads whatever the database holds, so asserting
    ``written > 0`` against ambient data passes on the operator's box and fails
    on an empty test database. Seed the precondition instead of assuming it.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memory_facts "
            "(fact_text, category, tenant_id, is_active, importance_score, entities) "
            "VALUES (%s, 'project', %s, TRUE, 0.99, %s) RETURNING id",
            (
                "Alice manages the Helios project at FakeVendorCo for the year.",
                DEFAULT_TENANT,
                ["Alice", "Helios"],
            ),
        )
        fid = cur.fetchone()[0]
        conn.commit()
    yield fid
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM memory_facts WHERE id = %s", (fid,))
        conn.commit()


@pytest.mark.integration
class TestProjectWritesRealFiles:
    def test_writes_notes_and_a_readme(self, tmp_path, monkeypatch, a_projectable_fact):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        result = project(limit=5)
        out = tmp_path / "brain" / "memory" / "vault"
        assert result["written"] > 0
        assert (out / "README.md").exists()
        assert len(list(out.glob("*.md"))) == result["written"] + 1

    def test_rerun_removes_its_own_stale_notes_but_not_the_operators(
        self, tmp_path, monkeypatch, a_projectable_fact
    ):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        out = tmp_path / "brain" / "memory" / "vault"
        project(limit=5)

        mine = out / "zzz-stale-generated-999999.md"
        mine.write_text(f"---\ngenerator: {GENERATED_MARKER}\n---\nold\n")
        theirs = out / "zzz-operators-own-note.md"
        theirs.write_text("# my own thinking, not yours\n")

        project(limit=5)

        assert not mine.exists(), "a stale generated note must be swept"
        assert theirs.exists(), "an operator's own note must never be deleted"
        assert theirs.read_text().startswith("# my own thinking")

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        result = project(limit=3, dry_run=True)
        assert result["dry_run"] is True
        assert not (tmp_path / "brain" / "memory" / "vault").exists()

    def test_usage_report_says_unread_when_it_is(self, tmp_path, monkeypatch, a_projectable_fact):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        project(limit=3)
        rep = projection_usage_report()
        assert rep["exists"] is True
        assert rep["notes"] > 0
        # Built to fail loudly: freshly written, nothing has read it.
        assert rep["opened"] == 0
        assert "delete" in rep["verdict"]

    def test_usage_report_on_a_missing_dir_is_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "nope"))
        assert projection_usage_report()["exists"] is False
