#!/usr/bin/env python3
"""Promote a live Microsoft 365 capture into FakeGraphTenant fixtures -- scrubbed twice.

The live smoke suite (``GENUS_LIVE_M365_CAPTURE=<dir>``) writes one scrubbed
JSON file per Graph exchange under ``<dir>/<test name>/``. This script:

1. scrubs every record AGAIN with a fresh salt (the scrubber is idempotent, so
   anything the first pass missed gets a second chance and ids stay joined);
2. runs the leak gate (:func:`robothor.workspace.microsoft.capture.find_leaks`):
   no JWT, no bearer token, no address outside ``*.example``, no unscrubbed
   GUID, no ``onmicrosoft.com`` name, no IP outside TEST-NET, and none of the
   ``--forbid`` literals (pass the tenant's domains and directory id). One leak
   and NOTHING is written;
3. writes ``<out>/<test name>.json``: the test's exchanges in order, ready for
   ``FakeGraphTenant.replay(load_capture_fixture(path))``.

Token-endpoint exchanges are kept in the fixture for the record (which
assertion shape Entra accepted) but ``replay`` skips them: the fake tenant
answers Entra itself.

Usage::

    python scripts/m365_capture_to_fixtures.py <capture-dir> <out-dir> \\
        --forbid contoso.com --forbid <directory-id> [--only test_name] [--check]

``--check`` runs the scrub and the leak gate and writes nothing.
Exit 0 = promoted (or clean under --check), 1 = a leak or an unreadable record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.workspace.microsoft.capture import SCHEMA, Scrubber, find_leaks  # noqa: E402

#: Environment values that must never survive into a fixture, when present.
_ENV_FORBIDDEN = (
    "GENUS_LIVE_M365_TENANT_ID",
    "GENUS_LIVE_M365_CLIENT_ID",
    "GENUS_LIVE_M365_ASSISTANT",
    "GENUS_LIVE_M365_OWNER",
    "GENUS_LIVE_M365_CANARY",
)


def _forbidden(extra: list[str], env: dict[str, str]) -> list[str]:
    out = list(extra)
    for name in _ENV_FORBIDDEN:
        value = env.get(name, "").strip()
        if value:
            out.append(value)
            if "@" in value:
                out.append(value.partition("@")[2])
    out.extend(env.get("GENUS_LIVE_M365_TENANTS", "").replace(",", " ").split())
    return [v for v in out if v]


def load_records(capture_dir: Path, only: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """``{test name: [record, ...]}`` in sequence order. Raises ValueError on a bad record."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(capture_dir.rglob("*.json")):
        if path.parent.name == "samples":
            continue
        try:
            record = json.loads(path.read_text())
        except ValueError:
            raise ValueError(f"{path.name}: not JSON") from None
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError(f"{path.name}: not a schema-{SCHEMA} capture record")
        test = str(record.get("test") or path.parent.name)
        if only and test != only:
            continue
        groups.setdefault(test, []).append(record)
    for records in groups.values():
        records.sort(key=lambda r: int(r.get("seq") or 0))
    return groups


def promote(
    capture_dir: Path,
    out_dir: Path,
    *,
    forbid: list[str],
    only: str | None = None,
    check: bool = False,
    salt: bytes | None = None,
) -> tuple[list[Path], list[str]]:
    """Scrub, gate, write. Returns ``(written paths, leaks)``; writes nothing on a leak."""
    scrubber = Scrubber(salt=salt or secrets.token_bytes(32), forbidden=forbid)
    groups = load_records(capture_dir, only)
    fixtures: dict[str, list[dict[str, Any]]] = {}
    leaks: list[str] = []
    for test, records in groups.items():
        cleaned = []
        for record in records:
            again = scrubber.rescrub(record)
            again.pop("seq", None)
            cleaned.append(again)
        leaks += find_leaks(cleaned, forbidden=forbid, where=test)
        fixtures[test] = cleaned
    if leaks or check:
        return [], leaks
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for test, cleaned in fixtures.items():
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", test)[:120] or "capture"
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written, leaks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="a literal that must not appear in a fixture (repeatable)",
    )
    parser.add_argument("--only", help="promote one test's capture")
    parser.add_argument("--check", action="store_true", help="scrub and gate, write nothing")
    args = parser.parse_args(argv)
    if not args.capture_dir.is_dir():
        print(f"no capture directory at {args.capture_dir}", file=sys.stderr)
        return 1
    try:
        written, leaks = promote(
            args.capture_dir,
            args.out_dir,
            forbid=_forbidden(args.forbid, dict(os.environ)),
            only=args.only,
            check=args.check,
        )
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if leaks:
        print(f"refused: {len(leaks)} leak(s); nothing was written:", file=sys.stderr)
        for leak in leaks[:50]:
            print(f"  {leak}", file=sys.stderr)
        return 1
    if args.check:
        print("clean: no leaks found")
        return 0
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
