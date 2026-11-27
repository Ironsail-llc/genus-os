"""The extraction prompt's "today" can be pinned for back-dated content.

Relative dates in a transcript ("yesterday", "next week") are resolved against
the date in the prompt. A loader of history recorded on another day (the
memory evaluation's public benchmarks) pins it to that day; everything else
keeps the wall clock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from robothor.memory import facts


def test_the_prompt_uses_today_by_default():
    prompt = facts.build_extraction_prompt("Alice met Bob yesterday.")
    assert f"relative to {datetime.now(UTC).date().isoformat()}" in prompt


def test_a_pinned_reference_date_is_used_and_released():
    with facts.extraction_reference_date("2023-05-20"):
        assert "relative to 2023-05-20" in facts.build_extraction_prompt("Alice met Bob.")
    assert "relative to 2023-05-20" not in facts.build_extraction_prompt("Alice met Bob.")


def test_an_explicit_argument_still_wins():
    with facts.extraction_reference_date("2023-05-20"):
        prompt = facts.build_extraction_prompt("x", reference_date="2024-01-02")
    assert "relative to 2024-01-02" in prompt


def test_the_pin_reaches_the_extraction_model_through_a_timeout(monkeypatch):
    seen: list[str] = []

    async def fake_generate(*, prompt, **_k):
        seen.append(prompt)
        return '{"facts": []}'

    monkeypatch.setattr(facts.generation, "generate", fake_generate)

    async def _run():
        with facts.extraction_reference_date("2022-02-02"):
            await facts.extract_facts_detailed("Alice met Bob last week.", max_retries=1)

    asyncio.run(_run())
    assert seen and "relative to 2022-02-02" in seen[0]
