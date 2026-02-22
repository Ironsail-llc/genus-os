"""Tests for robothor.cli — command line interface."""

from robothor.cli import main


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
        assert "Workspace" in out
        assert "Database" in out

    def test_no_args(self, capsys):
        rc = main([])
        assert rc == 0

    def test_migrate(self, capsys):
        rc = main(["migrate"])
        assert rc == 0

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
