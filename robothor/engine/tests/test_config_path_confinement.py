"""An agent id names a file inside the manifest directory, and nothing else.

``load_agent_config`` and ``explain_config`` turn an agent id that can come
from a request (manual trigger, the rooms layer, the explain endpoint) into a
manifest path. A legitimate id loads exactly the file it always did, from an
absolute, a relative or the current directory. Anything that could reach
outside the directory (a separator, ``..``, an absolute path, an empty id, a
control character) is refused before any file is opened.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from robothor.engine import config

REFUSED = ["../evil", "..", "a/b", "/etc/passwd", "", "x.yaml", "a\\b", "a\nb", "a b", ".hidden"]


def _manifest(directory: Path, agent_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{agent_id}.yaml").write_text(
        f"id: {agent_id}\nname: {agent_id.title()}\nmodel:\n  primary: openai/test\n"
    )


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    _manifest(agents, "analyst")
    _manifest(tmp_path, "evil")  # one level up: never reachable by id
    return agents


def test_a_legitimate_id_loads_its_manifest(fleet: Path) -> None:
    loaded = config.load_agent_config("analyst", fleet)
    assert loaded is not None and loaded.id == "analyst"


def test_a_relative_and_the_current_directory_still_work(fleet: Path, monkeypatch) -> None:
    monkeypatch.chdir(fleet.parent)
    assert config.load_agent_config("analyst", Path("agents")) is not None
    monkeypatch.chdir(fleet)
    assert config.load_agent_config("analyst", Path()) is not None


def test_the_manifest_path_is_the_one_it_always_was(fleet: Path, monkeypatch) -> None:
    """The path handed to the loader is byte-for-byte ``manifest_dir / <id>.yaml``."""
    seen: list[str] = []
    real = config.load_manifest

    def spy(path: Path) -> dict | None:  # type: ignore[type-arg]
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(config, "load_manifest", spy)
    config.load_agent_config("analyst", fleet)
    monkeypatch.chdir(fleet.parent)
    config.load_agent_config("analyst", Path("agents"))
    assert seen == [str(fleet / "analyst.yaml"), str(Path("agents") / "analyst.yaml")]


@pytest.mark.parametrize("agent_id", REFUSED)
def test_an_id_that_could_leave_the_directory_is_refused_unread(
    fleet: Path, monkeypatch, agent_id: str
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(config, "load_manifest", lambda p: opened.append(str(p)))
    assert config.load_agent_config(agent_id, fleet) is None
    assert opened == []


@pytest.mark.parametrize("agent_id", REFUSED)
def test_explain_refuses_the_same_ids(fleet: Path, agent_id: str) -> None:
    with pytest.raises(ValueError):
        config.explain_config(agent_id, fleet)


def test_explain_reads_a_legitimate_manifest(fleet: Path) -> None:
    explained = config.explain_config("analyst", fleet)
    assert explained["layers"]["agent_manifest"]["id"] == "analyst"


def test_a_manifest_load_failure_logs_a_path_with_no_line_breaks(tmp_path: Path, caplog) -> None:
    odd = tmp_path / "line\nbreak.yaml"
    odd.write_text("id: [unclosed\n")
    with caplog.at_level("ERROR"):
        config._load_manifest_classified(odd)
    assert caplog.records and all("\n" not in r.getMessage() for r in caplog.records)
    assert os.sep in caplog.records[-1].getMessage()
