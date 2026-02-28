"""Tests for robothor.cli — command line interface."""

from unittest.mock import MagicMock, patch

from robothor.cli import REQUIRED_TABLES, _find_migration_sql, main


class TestCli:
    def test_version(self, capsys):
        rc = main(["version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "robothor" in out
        assert "0.1.0" in out

    def test_version_flag(self, capsys):
        rc = main(["--version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "robothor" in out

    def test_status(self, capsys):
        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PostgreSQL" in out
        assert "Redis" in out
        assert "Ollama" in out

    def test_no_args(self, capsys):
        """No args launches TUI — mock _cmd_tui to avoid blocking."""
        with patch("robothor.cli._cmd_tui", return_value=0) as mock_tui:
            rc = main([])
            assert rc == 0
            mock_tui.assert_called_once()

    def test_pipeline(self, capsys):
        rc = main(["pipeline", "--tier", "1"])
        assert rc == 0

    def test_pipeline_tier_2(self, capsys):
        rc = main(["pipeline", "--tier", "2"])
        assert rc == 0

    def test_serve_without_uvicorn(self, capsys, monkeypatch):
        """If uvicorn isn't installed, serve should return 1 with error."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "uvicorn":
                raise ImportError("no uvicorn")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        rc = main(["serve"])
        assert rc == 1
        assert "uvicorn" in capsys.readouterr().out.lower()

    def test_mcp_without_mcp_lib(self, capsys, monkeypatch):
        """If mcp library isn't installed, mcp command should return 1."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "robothor.api.mcp":
                raise ImportError("no mcp")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        rc = main(["mcp"])
        assert rc == 1
        assert "mcp" in capsys.readouterr().out.lower()


class TestMigrate:
    def test_find_migration_sql(self):
        """Migration SQL should be findable (at least in dev layout)."""
        sql = _find_migration_sql()
        assert sql is not None
        assert "CREATE TABLE" in sql
        assert "memory_facts" in sql

    def test_dry_run_prints_sql(self, capsys):
        rc = main(["migrate", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "CREATE TABLE" in out or "CREATE EXTENSION" in out

    def test_check_with_mocked_db(self, capsys):
        """--check should report all tables present when DB has them."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [(t,) for t in REQUIRED_TABLES]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg2.connect", return_value=mock_conn):
            rc = main(["migrate", "--check"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "required tables present" in out

    def test_check_reports_missing(self, capsys):
        """--check should report missing tables."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [("memory_facts",), ("memory_entities",)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg2.connect", return_value=mock_conn):
            rc = main(["migrate", "--check"])

        assert rc == 1
        out = capsys.readouterr().out
        assert "Missing tables" in out

    def test_required_tables_list(self):
        """Sanity check: REQUIRED_TABLES has key tables."""
        assert "memory_facts" in REQUIRED_TABLES
        assert "crm_people" in REQUIRED_TABLES
        assert "agent_memory_blocks" in REQUIRED_TABLES
        assert "audit_log" in REQUIRED_TABLES


class TestStatusProbes:
    def test_status_shows_connected_pg(self, capsys):
        """Status should show Connected when PG is reachable."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            ("PostgreSQL 16.2",),  # version
            (17,),  # table count
            ("0.6.0",),  # pgvector version
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("psycopg2.connect", return_value=mock_conn), \
             patch("redis.Redis") as mock_redis_cls, \
             patch("httpx.get") as mock_httpx_get:
            mock_redis_cls.return_value.info.side_effect = Exception("refused")
            mock_httpx_get.side_effect = Exception("refused")

            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Connected" in out
        assert "PostgreSQL 16.2" in out

    def test_status_shows_unreachable(self, capsys):
        """Status should show UNREACHABLE when services are down."""
        with patch("psycopg2.connect", side_effect=Exception("Connection refused")), \
             patch("redis.Redis") as mock_redis_cls, \
             patch("httpx.get") as mock_httpx_get:
            mock_redis_cls.return_value.info.side_effect = Exception("refused")
            mock_httpx_get.side_effect = Exception("refused")

            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "UNREACHABLE" in out
