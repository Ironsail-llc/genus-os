"""External service connectors.

A small, generic REST→MCP bridge (``rest_mcp_bridge``) that exposes any
HTTP/JSON API (API Platform / Hydra, OpenAPI, plain REST) as MCP tools over
stdio. The same bridge binary works for both Claude Code and the RoboThor
engine's business-adapter system — configure it entirely with environment
variables and point it at a service. See ``docs/CONNECTORS.md``.
"""
