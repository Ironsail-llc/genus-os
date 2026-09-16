"""A fingerprint identifies a credential without being a way to guess it.

CodeQL's ``py/weak-sensitive-data-hashing`` on the first cut was right, and for
a reason worth stating plainly: the digest was HMAC-SHA256 under a constant
compiled into the source. Against a 40-character random token that is fine.
Against a short or low-entropy secret it is no better than a bare hash, because
the key is in the repository — anyone holding a leaked fingerprint can compute
candidates offline exactly as fast as we can.

The key is now a random salt this instance writes on first use, so the same
credential fingerprints differently on every box and a fingerprint tells an
outsider nothing.

What must NOT change is the property every caller depends on: the same value
gives the same short string, here, across processes, for the life of the
instance.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

VALUE = "ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def instance(tmp_path, monkeypatch):
    """A throwaway workspace, so no test touches the real instance's salt."""
    from robothor.secrets import fingerprint as module

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    module.reset_fingerprint_key()
    module._warned_unsalted = False
    yield tmp_path
    module.reset_fingerprint_key()


# ── the property callers depend on ───────────────────────────────────────────


def test_the_same_value_gives_the_same_fingerprint():
    from robothor.secrets.fingerprint import fingerprint

    assert fingerprint(VALUE) == fingerprint(VALUE)


def test_a_different_value_gives_a_different_fingerprint():
    from robothor.secrets.fingerprint import fingerprint

    assert fingerprint(VALUE) != fingerprint(VALUE + "x")


def test_it_survives_a_restart_of_the_process(instance):
    """Two processes of one instance must agree, or `vault_set`'s answer cannot
    be compared with a later `vault_get`'s."""
    from robothor.secrets import fingerprint as module

    first = module.fingerprint(VALUE)
    module.reset_fingerprint_key()  # a fresh process reads the salt again
    assert module.fingerprint(VALUE) == first


def test_the_fingerprint_reveals_nothing_of_the_value():
    from robothor.secrets.fingerprint import FINGERPRINT_PREFIX, fingerprint

    printed = fingerprint(VALUE)
    assert VALUE not in printed
    assert VALUE[:8] not in printed
    assert printed.startswith(FINGERPRINT_PREFIX)
    assert len(printed) == len(FINGERPRINT_PREFIX) + 8


def test_the_label_names_the_algorithm_actually_used():
    """A prefix naming the wrong algorithm is a small lie somebody eventually
    relies on — the digest is BLAKE2b, so the label is not `sha256:`."""
    from robothor.secrets.fingerprint import FINGERPRINT_PREFIX

    assert FINGERPRINT_PREFIX == "b2:"


# ── what the keying buys ─────────────────────────────────────────────────────


def test_two_instances_fingerprint_the_same_credential_differently(tmp_path, monkeypatch):
    """The whole point of the per-instance salt.

    With a key compiled into the source, a leaked fingerprint could be checked
    against a guessed value by anyone with the repository. It cannot be now.
    """
    from robothor.secrets import fingerprint as module

    one = tmp_path / "instance-one"
    two = tmp_path / "instance-two"
    one.mkdir()
    two.mkdir()

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(one))
    module.reset_fingerprint_key()
    first = module.fingerprint(VALUE)

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(two))
    module.reset_fingerprint_key()
    second = module.fingerprint(VALUE)

    assert first != second, "the same credential fingerprints identically on two boxes"
    module.reset_fingerprint_key()


def test_the_salt_is_written_once_and_readable_only_by_its_owner(instance):
    from robothor.secrets.fingerprint import _SALT_FILENAME, fingerprint

    fingerprint(VALUE)
    salt = Path(instance) / _SALT_FILENAME
    assert salt.is_file()
    assert len(salt.read_bytes()) >= 16
    mode = stat.S_IMODE(salt.stat().st_mode)
    assert mode == 0o600, f"the salt is mode {mode:04o}; another account could read it"

    before = salt.read_bytes()
    fingerprint("something else")
    assert salt.read_bytes() == before, "the salt was rewritten, so old fingerprints changed"


def test_the_salt_is_not_readable_by_an_agent():
    """`ROBOTHOR_WORKSPACE` is on the exec allowlist, so a child is told where
    the workspace is — the same reasoning that put `.vault-key` on this list."""
    from robothor.engine.secret_paths import exec_reads_secret, is_secret_path

    assert is_secret_path("/ws/.fingerprint-salt")
    assert exec_reads_secret("cat $ROBOTHOR_WORKSPACE/.fingerprint-salt") is not None


# ── degrading, not failing ───────────────────────────────────────────────────


def test_an_unwritable_workspace_still_produces_stable_fingerprints(tmp_path, monkeypatch, caplog):
    """A fingerprint is computed on the status path, in tool results and in the
    doctor. A filesystem problem must degrade it, never take those down — and
    must say so, because the degraded digest is computable from the source.
    """
    import logging

    from robothor.secrets import fingerprint as module

    unwritable = tmp_path / "read-only"
    unwritable.mkdir()
    unwritable.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(unwritable))
    module.reset_fingerprint_key()
    module._warned_unsalted = False

    try:
        with caplog.at_level(logging.INFO, logger="robothor.secrets.fingerprint"):
            first = module.fingerprint(VALUE)
        assert first == module.fingerprint(VALUE)
        assert VALUE not in first
        assert any("not private" in record.getMessage() for record in caplog.records), (
            "an instance whose fingerprints are not private must say so"
        )
    finally:
        unwritable.chmod(stat.S_IRWXU)
        module.reset_fingerprint_key()


# ── the salt file is placed atomically ───────────────────────────────────────


def test_an_empty_salt_file_does_not_key_the_instance_with_nothing(instance):
    """A1: the proof the reviewer ran.

    A process landing in the old create-then-write window read a zero-length
    file and keyed with `b""` for its life, so it agreed with nobody. An empty
    file is unusable to every process, so the right answer is to repair it, not
    to adopt it and not to degrade.
    """
    from robothor.secrets import fingerprint as module

    salt = Path(instance) / module._SALT_FILENAME
    salt.write_bytes(b"")

    key = module._read_or_create_salt()

    assert len(key) >= 16, "the instance keyed with an empty salt"
    assert key is not module._UNSALTED_FALLBACK, "a repairable file degraded the instance"
    assert salt.read_bytes() == key, "the repaired salt was not written back for other processes"


def test_a_short_salt_file_is_repaired_rather_than_used(instance):
    from robothor.secrets import fingerprint as module

    salt = Path(instance) / module._SALT_FILENAME
    salt.write_bytes(b"tooshort")

    key = module._read_or_create_salt()

    assert len(key) >= 16
    assert salt.read_bytes() == key


def test_an_existing_good_salt_is_never_replaced(instance):
    """The loser of a race adopts the winner's salt; it does not impose its own."""
    from robothor.secrets import fingerprint as module

    existing = b"x" * 32
    salt = Path(instance) / module._SALT_FILENAME
    salt.write_bytes(existing)

    assert module._read_or_create_salt() == existing
    assert salt.read_bytes() == existing


def test_the_salt_is_complete_before_it_appears_under_its_name(instance, monkeypatch):
    """No window in which another process can see a half-written salt.

    The content is written and fsynced to a temporary file in the same
    directory, then placed under the real name in one atomic step. This test
    watches that step: at the moment of placement the destination must not
    exist yet and the source must already hold the whole salt.
    """
    import os as real_os

    from robothor.secrets import fingerprint as module

    salt = Path(instance) / module._SALT_FILENAME
    observed: list[tuple[bool, int]] = []
    real_link = real_os.link

    def watched_link(source, destination, **kwargs):
        observed.append((Path(destination).exists(), Path(source).stat().st_size))
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "link", watched_link)
    key = module._read_or_create_salt()

    assert observed, "the salt was not placed with an atomic link"
    destination_existed, source_size = observed[0]
    assert not destination_existed, "the name existed before the content did"
    assert source_size == len(key) >= 16, "the content was placed before it was complete"
    assert salt.read_bytes() == key


def test_concurrent_first_use_agrees_on_one_salt(instance):
    """Eight processes starting at once is the ordinary case after a reboot."""
    import threading

    from robothor.secrets import fingerprint as module

    ready = threading.Barrier(8)
    answers: list[str] = []
    guard = threading.Lock()

    def one_process():
        ready.wait()
        digest = module.fingerprint(VALUE)
        with guard:
            answers.append(digest)

    threads = [threading.Thread(target=one_process) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(answers)) == 1, f"processes disagreed on the fingerprint: {sorted(set(answers))}"


# ── a degraded process finds its way back ────────────────────────────────────


def test_a_degraded_process_picks_the_salt_up_when_it_can(tmp_path, monkeypatch):
    """A2: degrading must be a state, not a verdict.

    The first cut cached the fallback key like any other, so a process that
    started while the volume was not mounted -- or before the directory was
    created -- kept fingerprinting with the published constant until somebody
    restarted it, while its neighbours used the real salt. Two live processes,
    two answers for one credential.
    """
    from robothor.secrets import fingerprint as module

    workspace = tmp_path / "late"
    workspace.mkdir()
    workspace.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setattr(module, "_DEGRADED_RETRY_SECONDS", 0.0)
    module.reset_fingerprint_key()
    module._warned_unsalted = False

    try:
        degraded = module.fingerprint(VALUE)

        workspace.chmod(stat.S_IRWXU)
        recovered = module.fingerprint(VALUE)
        assert recovered != degraded, "the process never looked for the salt again"

        # And it agrees with a process that started after the volume arrived.
        module.reset_fingerprint_key()
        assert module.fingerprint(VALUE) == recovered
    finally:
        workspace.chmod(stat.S_IRWXU)
        module.reset_fingerprint_key()


def test_a_salted_process_does_not_keep_looking(instance, monkeypatch):
    """The re-attempt is for the degraded state only: a fingerprint is computed
    on the status path and in every tool result, so the normal path must stay a
    cached constant with no filesystem call at all."""
    from robothor.secrets import fingerprint as module

    module.fingerprint(VALUE)

    def refuse(*args, **kwargs):
        raise AssertionError("a salted process went back to the filesystem")

    monkeypatch.setattr(module, "_read_or_create_salt", refuse)
    assert module.fingerprint(VALUE) == module.fingerprint(VALUE)


def test_the_degraded_state_is_not_re_checked_on_every_call(tmp_path, monkeypatch):
    """Bounded, because `fingerprint` is on hot paths: while degraded it looks
    again at most once per retry window, not once per call."""
    from robothor.secrets import fingerprint as module

    workspace = tmp_path / "missing"
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace / "nowhere" / "deeper"))
    monkeypatch.setattr(module, "_DEGRADED_RETRY_SECONDS", 3600.0)
    module.reset_fingerprint_key()
    module._warned_unsalted = False

    attempts = 0
    real = module._read_or_create_salt

    def counted():
        nonlocal attempts
        attempts += 1
        return real()

    monkeypatch.setattr(module, "_read_or_create_salt", counted)
    try:
        for _ in range(5):
            module.fingerprint(VALUE)
        assert attempts == 1, f"the filesystem was consulted {attempts} times"
    finally:
        module.reset_fingerprint_key()
