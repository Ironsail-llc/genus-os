"""A relative ``cd`` under ``CDPATH`` lands where the text does not say.

``CDPATH=~ cd keys`` moves to ``~/keys`` when it exists, not to ``./keys``, so
the exec gate cannot judge a bare key-file word by what the working directory
holds: ``cat prod.pem`` there reads the key the ``cd`` reached.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine import secret_paths


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "keys").mkdir(parents=True)
    (h / "keys" / "prod.pem").write_text("PEM")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CDPATH", raising=False)
    monkeypatch.chdir(ws)
    return h


def test_a_cdpath_assignment_makes_a_relative_cd_unknown(home: Path) -> None:
    assert secret_paths.exec_reads_secret("CDPATH=~ cd keys && cat prod.pem", cwd=str(Path.cwd()))


def test_an_exported_cdpath_makes_a_relative_cd_unknown(home: Path) -> None:
    command = "export CDPATH=~; cd keys && cat prod.pem"
    assert secret_paths.exec_reads_secret(command, cwd=str(Path.cwd()))


def test_a_cdpath_in_the_environment_makes_a_relative_cd_unknown(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CDPATH", str(home))
    assert secret_paths.exec_reads_secret("cd keys && cat prod.pem", cwd=str(Path.cwd()))


def test_a_dot_named_target_is_searched_on_cdpath_too(home: Path) -> None:
    (home / ".aws").mkdir()
    (home / ".aws" / "server.key").write_text("K")
    assert secret_paths.exec_reads_secret("CDPATH=~ cd .aws && cat server.key", cwd=str(Path.cwd()))


def test_without_cdpath_a_bare_key_word_that_names_no_file_still_runs(home: Path) -> None:
    assert secret_paths.exec_reads_secret("cd sub && jq -r .key", cwd=str(Path.cwd())) is None
