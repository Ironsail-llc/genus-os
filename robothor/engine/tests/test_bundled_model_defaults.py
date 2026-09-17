"""A default nobody passes a flag to must name a model that still exists.

`bench/wildclaw/agent.yaml` pinned `openrouter/xiaomi/mimo-v2-pro` until
2026-09-17. That id was withdrawn from OpenRouter (verified against the live
catalog on 2026-09-11, which is why the registry marks it `deprecated` and
names a replacement), so `python -m bench.wildclaw.harness` without `--model`
failed every task in seconds with "no answer from any model" — a whole sweep
reading as sixty capability failures.

The registry already knows which ids are dead. Nothing connected that
knowledge to the defaults that are used when no one passes an override, so a
retirement recorded in one file left the other silently broken.

This is the connection, and it is derived rather than listed: the retired set
comes from `list_models()` — which withholds `deprecated` entries — against
`list_models(include_deprecated=True)`. Mark an entry deprecated in the
registry and every bundled default naming it fails here on the same commit.

Scope is deliberately the defaults, not every mention: a manifest's
`model.primary` / `model.fallbacks`, and the hardcoded fallbacks in the
benchmark handler's grading path. A docstring may still discuss a dead model.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from robothor.engine.model_registry import list_models

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_DIR = REPO_ROOT / "bench"
BENCHMARK_HANDLER = REPO_ROOT / "robothor" / "engine" / "tools" / "handlers" / "benchmark.py"


def retired_model_ids() -> set[str]:
    """Registry ids no longer offered — `deprecated`, i.e. gone or withdrawn.

    Derived from the registry's own two listings rather than restated here, so
    the set follows the registry instead of drifting behind it.
    """
    offered = {row.model_id for row in list_models()}
    every = {row.model_id for row in list_models(include_deprecated=True)}
    return every - offered


def retired_ids_named_in(text: str) -> set[str]:
    """Which retired ids appear as a whole token in `text`."""
    found = set()
    for model_id in retired_model_ids():
        if re.search(rf"(?<![\w./:-]){re.escape(model_id)}(?![\w./:-])", text):
            found.add(model_id)
    return found


def _declared_models(manifest: dict) -> list[str]:
    model = manifest.get("model") or {}
    if not isinstance(model, dict):
        return []
    declared = [model.get("primary") or ""]
    declared.extend(model.get("fallbacks") or [])
    return [m for m in declared if isinstance(m, str) and m]


def _bench_manifests() -> list[Path]:
    return sorted(p for p in BENCH_DIR.rglob("*.yaml") if "tests" not in p.parts)


class TestTheGuardCanFail:
    """A scan that matches nothing is not a guard. Probe it before trusting it."""

    def test_the_registry_actually_withholds_something(self):
        assert retired_model_ids(), (
            "no registry entry is marked deprecated, so this whole file is inert"
        )

    def test_a_planted_retired_id_is_caught(self):
        dead = sorted(retired_model_ids())[0]
        assert retired_ids_named_in(f"  primary: {dead}\n") == {dead}

    def test_a_live_id_is_not_flagged(self):
        live = [row.model_id for row in list_models() if row.status == "current"][0]
        assert retired_ids_named_in(f"  primary: {live}\n") == set()

    def test_there_are_bench_manifests_to_scan(self):
        assert _bench_manifests(), "no bundled manifest found under bench/"


class TestBundledDefaultsNameALiveModel:
    def test_no_bench_manifest_pins_a_retired_model(self):
        offenders: dict[str, list[str]] = {}
        for path in _bench_manifests():
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(manifest, dict):
                continue
            dead = [m for m in _declared_models(manifest) if m in retired_model_ids()]
            if dead:
                offenders[str(path.relative_to(REPO_ROOT))] = dead
        assert not offenders, (
            f"bundled manifests name models the registry retired: {offenders}. "
            "Point them at the entry's `replaced_by`."
        )

    def test_the_benchmark_handler_hardcodes_no_retired_model(self):
        body = BENCHMARK_HANDLER.read_text(encoding="utf-8")
        defaults = [
            line for line in body.splitlines() if retired_ids_named_in(line) and ".get(" in line
        ]
        assert not defaults, f"hardcoded benchmark defaults name a retired model: {defaults}"
