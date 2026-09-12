"""The built-in checks, one module per category.

Each module exposes a module-level ``CHECKS`` tuple. That tuple is the only
thing :mod:`robothor.doctor.registry` reads, so adding a check means adding it
in one place, and a check that exists but is not in the tuple does not silently
half-exist.

Two rules every module here follows:

* **the ``run`` coroutine's docstring is operator-facing.** It is what someone
  reads after a red line, so it says what a failure MEANS and what to do, not
  how the function works. ``test_runner.py`` fails a check that has none.
* **no value ever reaches a result.** Names, sources, fingerprints, counts and
  status codes -- never a credential, never a connection string, never
  instruction text out of a manifest.
"""

from __future__ import annotations
