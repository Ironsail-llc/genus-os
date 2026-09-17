"""Files COPIED into a sandbox, never imported by the engine.

``genus_tools.py`` is written into the per-call directory an ``execute_code``
snippet runs from, so it has to work with nothing but the standard library and
must not name an engine module. It lives in the tree — rather than as a string
literal inside the handler — so that ruff, mypy and the test suite all read the
thing that actually ships. A client kept as a here-doc is a client nobody
lints.
"""

from __future__ import annotations
