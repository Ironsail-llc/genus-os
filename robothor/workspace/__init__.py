"""Provider-neutral workspace layer: mail and calendar over Google or Microsoft 365.

The ``gws_*`` tools keep their names and their guards; which backend serves
them is the ``workspace_provider`` setting (``google`` by default,
``microsoft365`` opt-in). This package holds the shared error vocabulary
(:mod:`robothor.workspace.errors`) and the per-provider transports
(:mod:`robothor.workspace.microsoft` for Microsoft Graph). See
``docs/workspace/microsoft365.md``.
"""
