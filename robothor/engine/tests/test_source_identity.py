"""A source-backed runtime cannot claim a revision after its code changes."""

import subprocess

import pytest


@pytest.fixture
def checkout(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    (tmp_path / "robothor").mkdir()
    (tmp_path / "robothor/__init__.py").write_text("VALUE = 1\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "initial",
    )
    return tmp_path, git("rev-parse", "HEAD")


def test_capture_ignores_untracked_instance_data_but_pins_clean_code(checkout):
    from robothor.engine.source_identity import SourceIdentity

    root, revision = checkout
    (root / "brain").mkdir()
    (root / "brain/status.md").write_text("Mutable instance state")
    identity = SourceIdentity.capture(root)
    identity.verify(revision)
    assert identity.revision == revision
    with pytest.raises(ValueError):
        identity.verify("a" * 40)


@pytest.mark.parametrize("change", ["edited", "restored", "untracked"])
def test_code_changes_require_a_new_runtime_generation(checkout, change):
    from robothor.engine.source_identity import SourceIdentity

    root, revision = checkout
    identity = SourceIdentity.capture(root)
    if change == "untracked":
        (root / "robothor/extra.py").write_text("VALUE = 9\n")
    else:
        path = root / "robothor/__init__.py"
        path.write_text("VALUE = 2\n")
        if change == "restored":
            path.write_text("VALUE = 1\n")
    with pytest.raises(ValueError):
        identity.verify(revision)


def test_dirty_or_non_git_installation_does_not_claim_source_identity(checkout, tmp_path):
    from robothor.engine.source_identity import SourceIdentity

    root, _ = checkout
    (root / "robothor/__init__.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError):
        SourceIdentity.capture(root)
    empty = tmp_path / "installed"
    empty.mkdir()
    with pytest.raises(ValueError):
        SourceIdentity.capture(empty)
