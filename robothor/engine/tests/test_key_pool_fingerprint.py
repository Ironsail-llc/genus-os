"""A pooled key is named in logs and alerts by the instance's keyed fingerprint.

``KeyPool.fingerprint`` printed a bare SHA-256 prefix, which an attacker
holding the label can test candidate keys against offline. The pool now
prints the same keyed, per-instance fingerprint as every other surface
(``key_fingerprint``). The label is used in memory only (logs, alerts, the
status the provider page matches on), never stored, so nothing persisted
depends on its old form.
"""

from __future__ import annotations

from robothor.engine.key_pool import KeyPool, key_fingerprint


def test_the_pool_names_a_key_as_every_other_surface_does() -> None:
    pool = KeyPool(["sk-alpha-0001", "sk-beta-0002"])
    assert pool.fingerprint("sk-alpha-0001") == key_fingerprint("sk-alpha-0001")
    assert [s.fingerprint for s in pool.status()] == [
        key_fingerprint("sk-alpha-0001"),
        key_fingerprint("sk-beta-0002"),
    ]


def test_the_label_carries_no_key_material() -> None:
    label = KeyPool(["sk-alpha-0001"]).fingerprint("sk-alpha-0001")
    assert "alpha" not in label and "0001" not in label
