"""Files COPIED into a sandbox, never RUN by the engine.

``genus_tools.py`` and ``http_recorder.py`` are written into the per-call
directory an ``execute_code`` snippet runs from, so they have to work with
nothing but the standard library and must not name an engine module. They
live in the tree — rather than as string literals inside the handler — so that
ruff, mypy and the test suite all read the thing that actually ships. A client
kept as a here-doc is a client nobody lints.

The engine imports nothing from ``genus_tools`` (its module level reads the
environment) and only the three constants from ``http_recorder`` (its module
level does nothing), so the file the sandbox runs and the name the engine
reads the record back under cannot drift apart.
"""

from __future__ import annotations
