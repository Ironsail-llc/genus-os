"""The startup secret check must actually check what it claims to.

2026-08-27: ``REQUIRED_KEYS`` listed ``OPENROUTER_API_KEY`` twice. The
fourth slot was a copy-paste of the first, so the validation that runs on
every boot verified the primary credential twice and nothing else. The
spare key the platform's own ``key_pool.py`` needs was absent from the
SOPS store for two days and no boot ever said so — which is why a module
written specifically to survive a dead key was running with one key.

A duplicate in a validation list is always a bug: it is a slot that was
meant to check something else.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decrypt-secrets.sh"


def _array(name: str) -> list[str]:
    text = SCRIPT.read_text()
    m = re.search(rf"^{name}=\((.*?)\n\)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


def test_required_keys_has_no_duplicates():
    keys = _array("REQUIRED_KEYS")
    assert keys, "REQUIRED_KEYS not found — did the script move?"
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicated slot(s) in REQUIRED_KEYS: {sorted(dupes)}"


def test_the_credential_spare_is_reported_on():
    """The spare need not be REQUIRED, but its absence must be visible."""
    text = SCRIPT.read_text()
    assert "OPENROUTER_API_KEY_2" in text, (
        "nothing in the boot path mentions the spare credential, so a pool of "
        "one stays silent — the precondition for the 2026-08-27 outage"
    )


def test_the_spare_is_advisory_not_required():
    """A missing spare must warn, never block a boot."""
    assert "OPENROUTER_API_KEY_2" not in _array("REQUIRED_KEYS")


def test_telegram_is_not_in_the_required_set():
    """Telegram is an optional channel (#498), so it cannot gate a boot."""
    keys = _array("REQUIRED_KEYS")
    for name in ("ROBOTHOR_TELEGRAM_BOT_TOKEN", "ROBOTHOR_TELEGRAM_CHAT_ID"):
        assert name not in keys, f"{name} blocks a boot, but Telegram is optional"


# ── What the required set actually does to a boot ─────────────────────────────
# Everything above reads the script's source. These RUN it: under
# ``ROBOTHOR_SECRETS_ROOT``, with a stub ``sops`` on ``ROBOTHOR_EXTRA_PATH`` that
# prints a fixture, so the validation is exercised rather than described.
#
# Telegram stayed in ``REQUIRED_KEYS`` long after it stopped being required
# anywhere else. A required key nothing needs is not a safety net: it is a fresh
# install that cannot boot because of a channel the operator never asked for —
# and since ``robothor-secrets.service`` orders four services, that is a whole
# instance in ``dependency failed`` with no ``Restart=`` to clear it.


def _run_decrypt(tmp_path: Path, payload: dict[str, str]):
    root = tmp_path / "root"
    (root / "etc" / "robothor").mkdir(parents=True)
    (root / "run").mkdir(parents=True)
    (root / "etc" / "robothor" / "secrets.enc.json").write_text("{}")
    (root / "etc" / "robothor" / "age.key").write_text("AGE-SECRET-KEY-STUB")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    sops = stub_bin / "sops"
    sops.write_text("#!/bin/bash\ncat <<'JSON'\n" + json.dumps(payload) + "\nJSON\n")
    sops.chmod(0o755)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ["PATH"],
            "ROBOTHOR_SECRETS_ROOT": str(root),
            "ROBOTHOR_EXTRA_PATH": str(stub_bin),
        },
    )
    return result, root / "run" / "robothor" / "secrets.env"


def test_a_store_without_telegram_credentials_boots(tmp_path: Path):
    result, output = _run_decrypt(
        tmp_path, {"OPENROUTER_API_KEY": "k", "OPENROUTER_API_KEY_2": "s"}
    )
    assert result.returncode == 0, (
        "a store with no Telegram credentials was refused — a fresh install "
        f"failing to boot for an optional channel\n{result.stdout}{result.stderr}"
    )
    assert 'OPENROUTER_API_KEY="k"' in output.read_text()


def test_a_store_without_the_provider_credential_is_refused_by_name(tmp_path: Path):
    """The one remaining required key is what the fleet cannot run without."""
    result, _ = _run_decrypt(tmp_path, {"SOMETHING_ELSE": "x"})
    assert result.returncode == 1, "a store with no provider credential was accepted"
    assert "OPENROUTER_API_KEY" in result.stderr, (
        "the refusal must name the missing key, or the operator cannot fix it"
    )


def test_a_missing_spare_warns_but_still_boots(tmp_path: Path):
    """2026-08-27: the pool ran with one key for two days and no boot said so."""
    result, _ = _run_decrypt(tmp_path, {"OPENROUTER_API_KEY": "k"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OPENROUTER_API_KEY_2" in result.stderr
    assert "no spare" in result.stderr
