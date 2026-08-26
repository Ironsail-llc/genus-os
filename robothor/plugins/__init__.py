"""The seam third parties extend the platform through.

Genus had no plugin system of any kind. A competitive audit of four agent
harnesses put it LAST on extensibility by a wide margin — DeepSeek Harness
is built on "everything is a plugin", OpenClaw and Hermes both have
ecosystems, and adding a tool here meant editing `dispatch._collect_handlers`
inside the engine. Every capability the platform gained was a fork.

A seam, not a marketplace. Discovery is `importlib.metadata` entry points,
which is how Python already does this: no registry to run, no package format
to invent, `pip install` is the install step.

    [project.entry-points."genus.tools"]       acme = "acme.tools:PLUGIN"
    [project.entry-points."genus.schemas"]     acme = "acme.tools:SCHEMA_PLUGIN"
    [project.entry-points."genus.guardrails"]  acme = "acme.policy:PLUGIN"
"""

from __future__ import annotations

from robothor.plugins.loader import (
    CONTRACT_VERSION,
    PluginFailure,
    PluginSet,
    load_plugins,
)

__all__ = ["CONTRACT_VERSION", "PluginFailure", "PluginSet", "load_plugins"]
