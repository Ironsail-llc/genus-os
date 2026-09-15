"""Publisher keys the platform pins, and the ones an instance pins itself.

An index is trustworthy because of WHOSE key signed it, and a key that arrives
with the document it signs proves nothing. So the set of acceptable keys is
pinned out of band, in two places:

* :data:`PUBLISHER_KEYS` — shipped with the platform, one entry per publisher
  the platform itself vouches for. **It is empty on purpose.** The production
  key for the Genus registry is minted by whoever runs that registry; a key
  committed here before it exists would be a placeholder that either never gets
  replaced or gets replaced by whoever opens the next pull request.
* ``ROBOTHOR_PLUGIN_INDEX_KEYS`` — a directory of PEM files on the instance.
  A company running its own internal registry drops its public key there; the
  file's stem is the ``key_id``. This is what makes a private plugin registry
  an operator decision rather than a platform change.

Only PUBLIC keys ever live here or there. Nothing in this package signs an
index except ``scripts/build_plugin_index.py``, which is handed a private key
path by whoever runs it and never stores one.
"""

from __future__ import annotations

#: ``key_id`` -> PEM-encoded Ed25519 **public** key. Empty until the Genus
#: registry's key is minted; see the module docstring for why.
PUBLISHER_KEYS: dict[str, str] = {}

__all__ = ["PUBLISHER_KEYS"]
