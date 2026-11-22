"""The real engine daemon for the workspaces dry run, with two stand-ins.

Development only, never shipped: ``scripts/dev/rooms-stack.sh`` starts it.
Everything is the production engine (``robothor.engine.daemon.run``) except:

* the model: ``fake_llm.install()`` answers every ``litellm.acompletion``
  from a script, so a run is deterministic and costs nothing;
* one extra read-only tool, ``sample_sales_figures`` (``fixture_tools``), added
  beside the built-in tools before the registry is first built, so a report's
  provenance names a real tool call over fixture data;
* the mail transport: an approved workspace email (``robothor.rooms.outbound``)
  is appended to ``<workspace>/outbox.jsonl`` instead of being sent, so no
  email can leave the machine and the dry run can count what was "sent".

Refuses to start unless ``GENUS_ENVIRONMENT`` is ``development`` and the
database name ends in ``_test``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fake_llm  # noqa: E402
import fixture_tools  # noqa: E402


def _refuse_outside_a_dry_run() -> None:
    from robothor.settings import get_settings

    settings = get_settings()
    if settings.auth.environment != "development" or not settings.database.name.endswith("_test"):
        sys.exit("rooms stack: refusing to start outside development on a *_test database")


def _add_fixture_tool() -> None:
    from robothor.engine.tools import dispatch, registry

    schemas = registry.get_engine_schemas

    def with_fixture() -> dict[str, Any]:
        return {**schemas(), fixture_tools.NAME: fixture_tools.SCHEMA}

    registry.get_engine_schemas = with_fixture
    handlers = dispatch.builtin_handlers

    def with_fixture_handler() -> dict[str, Any]:
        return {**handlers(), fixture_tools.NAME: fixture_tools.handle}

    dispatch.builtin_handlers = with_fixture_handler


def _fake_mail() -> None:
    import json
    import threading

    from robothor.engine.config import EngineConfig
    from robothor.rooms import outbound

    outbox = EngineConfig.from_env().workspace / "outbox.jsonl"
    lock = threading.Lock()

    def send_raw(tenant_id: str, raw: str) -> dict[str, Any]:
        with lock:
            lines = outbox.read_text(encoding="utf-8").count("\n") if outbox.exists() else 0
            with outbox.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tenant": tenant_id, "raw": raw}) + "\n")
        return {"id": f"dry-run-{lines + 1}", "threadId": f"dry-run-thread-{lines + 1}"}

    outbound.send_raw = send_raw


def main() -> None:
    _refuse_outside_a_dry_run()
    fake_llm.install()
    _add_fixture_tool()
    _fake_mail()
    from robothor.engine.daemon import run

    run()


if __name__ == "__main__":
    main()
