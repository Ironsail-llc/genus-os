#!/usr/bin/env python3
"""Drop a subchart's test-hook documents from a rendered manifest stream.

kube-linter should see everything this chart renders, including its own
helm-test hook (``templates/test-connection.yaml``) — that is first-party code
and was linted before the cache subchart arrived.

What it must not see is the subchart's test hook: upstream hardcodes that pod
with no resource limits, so ``unset-memory-requirements`` fails and no value of
ours can set them. ``--no-hooks`` removes it, but takes our own hook with it.

So filter by provenance instead of by kind. ``helm template`` prefixes each
document with ``# Source: <chart>/<path>``, which is enough to drop exactly the
documents a given subchart's ``tests/`` directory contributed.

Usage:
    helm template ... | strip_subchart_test_hooks.py <subchart-name> [...]
"""

from __future__ import annotations

import sys

SEPARATOR = "\n---\n"


def strip(stream: str, subcharts: list[str]) -> tuple[str, int]:
    needles = [f"/charts/{name}/templates/tests/" for name in subcharts]
    documents = stream.split(SEPARATOR)
    kept = [d for d in documents if not any(n in d for n in needles)]
    return SEPARATOR.join(kept), len(documents) - len(kept)


def main() -> int:
    subcharts = sys.argv[1:]
    if not subcharts:
        print("usage: strip_subchart_test_hooks.py <subchart-name> [...]", file=sys.stderr)
        return 2
    kept, dropped = strip(sys.stdin.read(), subcharts)
    sys.stdout.write(kept)
    print(f"dropped {dropped} test-hook document(s) from: {', '.join(subcharts)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
