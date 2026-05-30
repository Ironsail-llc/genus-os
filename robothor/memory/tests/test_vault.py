"""Tests for `robothor.memory.vault` — the verbatim Knowledge Vault.

DB and embeddings are mocked; AES round-trips use the real crypto with a
fixed test master key. These assert the safety invariants: high values are
encrypted at rest, search never exposes a value, reads are audited, and
tenant isolation holds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory import vault

_FAKE_EMBEDDING = [0.0] * 1024
_TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes


def _mock_conn(*, fetchone: object = None, fetchall: list | None = None, rowcount: int = 1):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = None
    return cm, cur


class TestStoreVaultEntry:
    @patch("robothor.memory.vault.get_connection")
    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_low_sensitivity_stores_plaintext(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchone=(7,))
        mock_conn.return_value = cm

        entry_id = await vault.store_vault_entry(
            "FakeVendorCo support line",
            "555-0142 ext 7",
            entry_type="contact_info",
            sensitivity="low",
            tenant_id="t1",
        )
        assert entry_id == 7
        params = cur.execute.call_args.args[1]
        # param order: tenant, entry_type, caption, value_exact at index 3, value_enc at index 4
        assert params[3] == "555-0142 ext 7"  # value_exact plaintext
        assert params[4] is None  # value_enc

    @patch("robothor.memory.vault.get_connection")
    @patch("robothor.vault.crypto.get_master_key", return_value=_TEST_KEY)
    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_high_sensitivity_encrypts_at_rest(
        self, mock_key: MagicMock, mock_conn: MagicMock
    ) -> None:
        cm, cur = _mock_conn(fetchone=(9,))
        mock_conn.return_value = cm

        secret = "sk-proj-SUPERSECRET-001"
        entry_id = await vault.store_vault_entry(
            "OpenAI prod key",
            secret,
            entry_type="api_key",
            sensitivity="high",
            tenant_id="t1",
        )
        assert entry_id == 9
        params = cur.execute.call_args.args[1]
        assert params[3] is None  # value_exact must be NULL for high
        value_enc = params[4]
        assert isinstance(value_enc, (bytes, bytearray))
        assert secret.encode() not in bytes(value_enc)  # not plaintext on the wire
        # ...and it round-trips back to the original
        assert vault._decrypt_value(value_enc) == secret

    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_rejects_bad_entry_type(self) -> None:
        with pytest.raises(ValueError):
            await vault.store_vault_entry("c", "v", entry_type="bogus", tenant_id="t1")

    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_rejects_bad_sensitivity(self) -> None:
        with pytest.raises(ValueError):
            await vault.store_vault_entry(
                "c", "v", entry_type="contact_info", sensitivity="secret", tenant_id="t1"
            )


class TestSearchVault:
    @patch("robothor.memory.vault.get_connection")
    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_returns_captions_without_values(self, mock_conn: MagicMock) -> None:
        rows = [
            {
                "id": 1,
                "caption": "support line",
                "entry_type": "contact_info",
                "sensitivity": "low",
                "source": "crm",
                "created_at": None,
                "similarity": 0.91,
            },
        ]
        cm, cur = _mock_conn(fetchall=rows)
        mock_conn.return_value = cm

        out = await vault.search_vault("support number", tenant_id="t1")
        assert out[0]["caption"] == "support line"
        assert "value" not in out[0]
        assert "value_exact" not in out[0]

    @patch("robothor.memory.vault.get_connection")
    @patch.object(
        vault.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_entry_type_filter_adds_param(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchall=[])
        mock_conn.return_value = cm

        await vault.search_vault("x", entry_type="api_key", limit=3, tenant_id="t1")
        sql, params = cur.execute.call_args.args
        assert "entry_type = %s" in sql
        # embedding, tenant, entry_type, embedding, limit
        assert params[2] == "api_key"
        assert params[-1] == 3


class TestGetVaultValue:
    @patch("robothor.memory.vault.get_connection")
    @patch("robothor.vault.crypto.get_master_key", return_value=_TEST_KEY)
    def test_decrypts_high_and_audits(self, mock_key: MagicMock, mock_conn: MagicMock) -> None:
        from robothor.vault.crypto import encrypt

        blob = encrypt("ACCT-HEL-00917", _TEST_KEY)
        row = {
            "id": 5,
            "caption": "Helios acct",
            "entry_type": "account_id",
            "sensitivity": "high",
            "value_exact": None,
            "value_enc": blob,
        }
        cm, cur = _mock_conn(fetchone=row)
        mock_conn.return_value = cm

        result = vault.get_vault_value(5, tenant_id="t1", run_id="r1", agent_id="main")
        assert result["value"] == "ACCT-HEL-00917"
        # two executes: the SELECT and the audit INSERT
        assert cur.execute.call_count == 2
        assert "vault_access_log" in cur.execute.call_args_list[1].args[0]

    @patch("robothor.memory.vault.get_connection")
    def test_not_found_returns_error(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchone=None)
        mock_conn.return_value = cm

        result = vault.get_vault_value(999, tenant_id="t1")
        assert result == {"error": "not_found", "id": 999}
        # no audit row written when nothing was found
        assert cur.execute.call_count == 1

    @patch("robothor.memory.vault.get_connection")
    def test_query_is_tenant_scoped(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchone=None)
        mock_conn.return_value = cm

        vault.get_vault_value(5, tenant_id="t-other")
        sql, params = cur.execute.call_args.args
        assert "tenant_id = %s" in sql
        assert params == (5, "t-other")


class TestDeactivate:
    @patch("robothor.memory.vault.get_connection")
    def test_returns_true_when_row_updated(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=1)
        mock_conn.return_value = cm
        assert vault.deactivate_entry(5, tenant_id="t1") is True

    @patch("robothor.memory.vault.get_connection")
    def test_returns_false_when_nothing_updated(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=0)
        mock_conn.return_value = cm
        assert vault.deactivate_entry(5, tenant_id="t1") is False


class TestHandlerGating:
    @pytest.mark.asyncio
    async def test_store_disabled_when_rip12_off(self) -> None:
        from robothor.engine.tools.handlers import memory_vault as h

        with patch.object(h, "is_rip_enabled", return_value=False):
            out = await h._vault_store({"caption": "c", "value": "v"}, MagicMock())
        assert "disabled" in out["error"]

    @pytest.mark.asyncio
    async def test_search_runs_when_rip12_on(self) -> None:
        from robothor.engine.tools.handlers import memory_vault as h

        ctx = MagicMock()
        ctx.tenant_id = "t1"
        with (
            patch.object(h, "is_rip_enabled", return_value=True),
            patch(
                "robothor.memory.vault.search_vault",
                new=AsyncMock(
                    return_value=[
                        {
                            "id": 1,
                            "caption": "c",
                            "entry_type": "contact_info",
                            "sensitivity": "low",
                            "similarity": 0.9,
                        }
                    ]
                ),
            ),
        ):
            out = await h._vault_search({"query": "x"}, ctx)
        assert out["results"][0]["caption"] == "c"
        assert "value" not in out["results"][0]
