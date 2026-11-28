"""A NUL character in a memory fact must not cost the fact.

Postgres ``text`` and ``varchar`` columns reject 0x00, and ``jsonb`` rejects the
``\\u0000`` escape that ``json.dumps`` writes for it. A fact whose extracted text
carried one used to fail the insert and be skipped. The write path now strips
NUL, and only NUL, from every string it writes, before the content hash and the
embedding are computed, so the stored row is self-consistent.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.facts as facts
from robothor.memory.drift import compute_fact_hash

TENANT = "test-tenant"


def _nul_fact() -> dict[str, Any]:
    return {
        "fact_text": "Alice moved the\x00 launch review to Friday.",
        "category": "event",
        "entities": ["Alice\x00", "launch review"],
        "confidence": 0.9,
    }


CLEAN_TEXT = "Alice moved the launch review to Friday."


class TestStripNul:
    def test_removes_only_nul(self) -> None:
        value, removed = facts._strip_nul("a\x00b\x00c")
        assert value == "abc"
        assert removed == 2

    def test_other_control_characters_and_unicode_are_kept(self) -> None:
        text = "tab\there\nnew\x01line\x1f café — \U0001f600"
        value, removed = facts._strip_nul(text)
        assert value == text
        assert removed == 0

    def test_nested_containers_keys_and_values(self) -> None:
        value, removed = facts._strip_nul(
            {"k\x00ey": ["v\x00", {"inner": "x\x00y"}], "n": 3, "none": None}
        )
        assert value == {"key": ["v", {"inner": "xy"}], "n": 3, "none": None}
        assert removed == 3

    def test_non_strings_pass_through(self) -> None:
        assert facts._strip_nul(0.5) == (0.5, 0)
        assert facts._strip_nul(None) == (None, 0)


def _mock_conn() -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.fetchone.return_value = (7,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm, cur


def _no_nul(params: tuple[Any, ...]) -> bool:
    return "\x00" not in repr(params) and "\\u0000" not in repr(params)


class TestStoreFactStripsNul:
    @pytest.mark.asyncio
    async def test_written_params_carry_no_nul(self, caplog: Any) -> None:
        cm, cur = _mock_conn()
        fact = _nul_fact()
        with (
            patch("robothor.memory.facts.get_connection", return_value=cm),
            patch(
                "robothor.memory.facts.llm_client.get_embedding_async",
                AsyncMock(return_value=[0.1] * 4),
            ) as embed,
            caplog.at_level(logging.WARNING, logger="robothor.memory.facts"),
        ):
            fact_id = await facts.store_fact(
                fact, "source\x00 text", "conver\x00sation", {"note": "a\x00b"}, tenant_id=TENANT
            )

        assert fact_id == 7
        params = cur.execute.call_args_list[0].args[1]
        assert _no_nul(params)
        assert params[0] == CLEAN_TEXT
        assert params[2] == ["Alice", "launch review"]
        assert params[4] == "source text"
        assert params[5] == "conversation"
        assert params[7] == '{"note": "ab"}'
        # Hash and embedding are over the text actually stored, so the drift
        # check on a later update does not see a mismatch.
        assert params[9] == compute_fact_hash(CLEAN_TEXT, tenant_id=TENANT, category="event")
        embed.assert_awaited_once_with(CLEAN_TEXT)
        # The caller's dict is not mutated.
        assert fact["fact_text"].count("\x00") == 1
        # One content-free log line with the count.
        lines = [r for r in caplog.records if "NUL" in r.getMessage()]
        assert len(lines) == 1
        assert "5" in lines[0].getMessage()
        assert "Alice" not in lines[0].getMessage()

    @pytest.mark.asyncio
    async def test_clean_fact_logs_nothing(self, caplog: Any) -> None:
        cm, _cur = _mock_conn()
        with (
            patch("robothor.memory.facts.get_connection", return_value=cm),
            patch(
                "robothor.memory.facts.llm_client.get_embedding_async",
                AsyncMock(return_value=[0.1] * 4),
            ),
            caplog.at_level(logging.WARNING, logger="robothor.memory.facts"),
        ):
            await facts.store_fact(
                {"fact_text": CLEAN_TEXT, "category": "event", "entities": ["Alice"]},
                "src",
                "conversation",
                tenant_id=TENANT,
            )
        assert not [r for r in caplog.records if "NUL" in r.getMessage()]


class TestStoreFactsBatchStripsNul:
    @pytest.mark.asyncio
    async def test_written_params_carry_no_nul(self, caplog: Any) -> None:
        cm, cur = _mock_conn()
        with (
            patch("robothor.memory.facts.get_connection", return_value=cm),
            patch(
                "robothor.memory.facts.llm_client.get_embeddings_batch_async",
                AsyncMock(return_value=[[0.1] * 4]),
            ) as embed,
            caplog.at_level(logging.WARNING, logger="robothor.memory.facts"),
        ):
            ids = await facts.store_facts_batch(
                [_nul_fact()], "source\x00 text", "conversation", {"k": "v\x00"}, tenant_id=TENANT
            )

        assert ids == [7]
        params = cur.execute.call_args_list[0].args[1]
        assert _no_nul(params)
        assert params[0] == CLEAN_TEXT
        assert params[9] == compute_fact_hash(CLEAN_TEXT, tenant_id=TENANT, category="event")
        embed.assert_awaited_once_with([CLEAN_TEXT])
        lines = [r for r in caplog.records if "NUL" in r.getMessage()]
        assert len(lines) == 1
        assert "4" in lines[0].getMessage()


@pytest.mark.integration
class TestNulFactReachesPostgres:
    """Through the real insert into a real ``*_test`` database."""

    @pytest.fixture(autouse=True)
    def _stub_embeddings(self, monkeypatch: Any) -> None:
        async def _one(text: str, *args: Any, **kwargs: Any) -> list[float]:
            return [0.0] * 1024

        async def _many(texts: list[str], *args: Any, **kwargs: Any) -> list[list[float]]:
            return [[0.0] * 1024 for _ in texts]

        monkeypatch.setattr("robothor.llm.ollama.get_embedding_async", _one)
        monkeypatch.setattr("robothor.llm.ollama.get_embeddings_batch_async", _many)

    @pytest.fixture(autouse=True)
    def _tenant(self, db_conn: Any) -> None:
        # Inside the per-test transaction, so it is rolled back with the rows.
        db_conn.cursor().execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (TENANT, "Test Tenant"),
        )

    @staticmethod
    def _row(db_conn: Any, fact_id: int) -> tuple[Any, ...]:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT fact_text, entities, source_content, metadata FROM memory_facts WHERE id = %s",
            (fact_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return tuple(row)

    @pytest.mark.asyncio
    async def test_store_fact(self, mock_get_connection: Any, db_conn: Any) -> None:
        fact_id = await facts.store_fact(
            _nul_fact(), "source\x00 text", "conversation", {"note": "a\x00b"}, tenant_id=TENANT
        )
        assert fact_id > 0
        text, entities, source, metadata = self._row(db_conn, fact_id)
        assert text == CLEAN_TEXT
        assert entities == ["Alice", "launch review"]
        assert source == "source text"
        assert metadata == {"note": "ab"}

    @pytest.mark.asyncio
    async def test_store_facts_batch(self, mock_get_connection: Any, db_conn: Any) -> None:
        ids = await facts.store_facts_batch(
            [_nul_fact()], "source\x00 text", "conversation", tenant_id=TENANT
        )
        assert len(ids) == 1 and ids[0] > 0
        text, entities, source, _metadata = self._row(db_conn, ids[0])
        assert text == CLEAN_TEXT
        assert entities == ["Alice", "launch review"]
        assert source == "source text"
