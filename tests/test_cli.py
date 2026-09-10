"""Tests for robothor.cli — command line interface."""

from unittest.mock import MagicMock, patch

from robothor import __version__
from robothor.cli import REQUIRED_TABLES, main


class TestCli:
    def test_version(self, capsys):
        rc = main(["version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "robothor" in out
        assert __version__ in out

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
    def test_status_reports_the_manifest_size(self, capsys):
        """`migrate --status` must say how many migrations the manifest holds."""
        rows = [
            {
                "migration_id": "001_init",
                "version": "001",
                "filename": "001_init.sql",
                "source": "infra",
                "status": "applied",
                "applied_at": None,
            }
        ]
        with (
            patch("psycopg2.connect", return_value=MagicMock()),
            patch("robothor.db.migrate.status", return_value=rows),
            patch("robothor.db.migrate.manifest_count", return_value=113),
        ):
            rc = main(["migrate", "--status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "113" in out
        assert "001_init" in out

    def test_adopt_baseline_flag_reaches_the_migrator(self):
        """The flag that skips re-running 001_init must be forwarded, not swallowed."""
        mock_conn = MagicMock()
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.apply", return_value=[]) as mock_apply,
            patch("robothor.cli.admin.cmd_migrate_check", return_value=0),
        ):
            rc = main(["migrate", "--adopt-baseline"])

        assert rc == 0
        mock_apply.assert_called_once_with(
            connection=mock_conn, adopt_baseline=True, adopt_through=None
        )

    def test_adopt_through_flag_reaches_the_migrator(self):
        """The bound on how far adoption goes must survive the CLI boundary."""
        mock_conn = MagicMock()
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.apply", return_value=[]) as mock_apply,
            patch("robothor.cli.admin.cmd_migrate_check", return_value=0),
        ):
            rc = main(["migrate", "--adopt-through", "040_memory_episodes"])

        assert rc == 0
        mock_apply.assert_called_once_with(
            connection=mock_conn, adopt_baseline=False, adopt_through="040_memory_episodes"
        )

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

        current = [{"migration_id": "001_init", "status": "applied"}]
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.status", return_value=current),
        ):
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

        current = [{"migration_id": "001_init", "status": "applied"}]
        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("robothor.db.migrate.status", return_value=current),
        ):
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

        with (
            patch("psycopg2.connect", return_value=mock_conn),
            patch("redis.Redis") as mock_redis_cls,
            patch("httpx.get") as mock_httpx_get,
        ):
            mock_redis_cls.return_value.info.side_effect = Exception("refused")
            mock_httpx_get.side_effect = Exception("refused")

            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Connected" in out
        assert "PostgreSQL 16.2" in out

    def test_status_shows_unreachable(self, capsys):
        """Status should show UNREACHABLE when services are down."""
        with (
            patch("psycopg2.connect", side_effect=Exception("Connection refused")),
            patch("redis.Redis") as mock_redis_cls,
            patch("httpx.get") as mock_httpx_get,
        ):
            mock_redis_cls.return_value.info.side_effect = Exception("refused")
            mock_httpx_get.side_effect = Exception("refused")

            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "UNREACHABLE" in out
