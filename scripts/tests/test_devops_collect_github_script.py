"""scripts/devops_collect_github.py: writes the run file even when it fails, exits non-zero."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_collect_github.py"


def _load():
    spec = importlib.util.spec_from_file_location("devops_collect_github_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_collect_github_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_failed_repo_still_writes_json_and_exits_1(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DEVOPS_GITHUB_REPOS", "acme/widgets")

    async def fake_collect(repos, start, end):
        return {
            "repos": {
                "acme/widgets": {
                    "unavailable": {"prs": "401"},
                    "merged_prs": [],
                    "reviews": [],
                    "commits": {"by_login": {}, "unlinked": {}},
                    "stale_prs": [],
                }
            },
            "errors": [
                {"repo": "acme/widgets", "endpoint": "/repos", "status": 401, "message": "Bad"}
            ],
            "complete": False,
        }

    monkeypatch.setattr(mod, "_collect", fake_collect)
    rc = mod.main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)])
    assert rc == 1
    data = json.loads((tmp_path / "2026-05-11" / "github.json").read_text())
    assert data["errors"][0]["status"] == 401
    assert data["period"]["week_start"] == "2026-05-11"


def test_success_exits_0(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DEVOPS_GITHUB_REPOS", "acme/widgets")

    async def fake_collect(repos, start, end):
        return {"repos": {}, "errors": [], "complete": True}

    monkeypatch.setattr(mod, "_collect", fake_collect)
    assert mod.main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)]) == 0
