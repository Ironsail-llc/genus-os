"""Three research norms, added after a controlled competitive loss.

WildClawBench Search & Retrieval, GLM 5.2 on both sides: two tasks scored
0 for Genus and 100 for OpenClaw, and the full-transcript diff showed the
gap was norms, not model. Our agent (a) concluded "no shorter path exists"
from a source that structurally could not contain one, (b) relied on a
statute its own tool output showed was superseded, without ever asking
whether it was current, and (c) scraped rendered pages one at a time for a
relational question a structured source answers in one query.

Each rule is deliberately generic — it names no site, no API, no domain.
Task-specific coaching would be benchmark gaming; these are the norms any
research-shaped production task needs (the current price, the most recent
invoice, all open tasks).
"""

from __future__ import annotations

from robothor.engine.prompts import BEHAVIORAL_RULES


class TestResearchNorms:
    def test_source_currency_is_a_rule(self):
        """A dated/versioned source must be checked for currency before use."""
        assert "currently governs" in BEHAVIORAL_RULES
        assert "superseded" in BEHAVIORAL_RULES

    def test_negative_claims_need_named_exhausted_sources(self):
        """'X does not exist' from a partial scan is the signature honesty
        failure — absence in a listing is not absence in the world."""
        assert "not evidence it does not exist" in BEHAVIORAL_RULES

    def test_relational_questions_prefer_structured_sources(self):
        assert "structured source" in BEHAVIORAL_RULES

    def test_rules_stay_numbered_and_well_formed(self):
        """The block is consumed as a numbered list; a malformed insert would
        silently degrade every system prompt on the fleet."""
        import re

        numbers = [int(m) for m in re.findall(r"^(\d+)\. \*\*", BEHAVIORAL_RULES, re.MULTILINE)]
        assert numbers == list(range(1, len(numbers) + 1)), numbers

    def test_the_assembled_prompt_is_also_sequential(self, monkeypatch):
        """HONEST_CLAIMS_RULE is appended behind a flag with its own number —
        adding a rule to the base list without renumbering it produces two
        rules with the same number in every enforce-mode system prompt."""
        import re

        monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_MODE", "enforce")
        from robothor.engine.prompts import behavioral_rules

        text = behavioral_rules()
        numbers = [int(m) for m in re.findall(r"^(\d+)\. \*\*", text, re.MULTILINE)]
        assert numbers == list(range(1, len(numbers) + 1)), numbers
