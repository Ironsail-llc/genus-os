"""Every table keyed on a trigger type, and where ``CHANNEL`` belongs in it.

A new ``TriggerType`` member is a decision in each of these, and the failure mode
is uniform: the member is simply absent, nothing raises, and the run is quietly
treated as the thing it is not. ``migration 097`` exists because that happened to
``slack``, ``webhook``, ``ide`` and ``channel_event`` at the database CHECK.

Two kinds of table, and they are decided differently:

* **admission priority** — a Teams message is a person waiting, exactly like a
  Slack message, and Slack is already in that set. Its absence was a miss;
* **the warmup, person-linkage and verification tables** — those list
  ``TELEGRAM, WEBCHAT`` and exclude ``SLACK`` too. A plugin channel is Slack's
  peer there (a shared session, no chat-id detail to resolve a person from), so
  it is excluded *deliberately*. Pinned below so that a future change which adds
  one of the two without the other has to say why.
"""

from __future__ import annotations

from robothor.engine.models import TriggerType
from robothor.engine.pool import Priority


class TestAdmission:
    def test_a_plugin_channel_message_is_interactive(self):
        """``Priority.INTERACTIVE`` bypasses admission entirely — a human is
        waiting, and queueing them behind a nightly CRM sweep is the inversion
        that exists to prevent. Without this, a Teams run competed as CRITICAL
        (for main) or BACKGROUND (for anything else)."""
        from robothor.engine.agent_priority import classify

        assert classify("main", TriggerType.CHANNEL) is Priority.INTERACTIVE
        assert classify("some-worker", TriggerType.CHANNEL) is Priority.INTERACTIVE

    def test_it_is_classified_exactly_as_slack_is(self):
        from robothor.engine.agent_priority import classify

        for agent in ("main", "some-worker"):
            assert classify(agent, TriggerType.CHANNEL) is classify(agent, TriggerType.SLACK)

    def test_a_cron_run_is_still_not_interactive(self):
        from robothor.engine.agent_priority import classify

        assert classify("some-worker", TriggerType.CRON) is not Priority.INTERACTIVE


class TestTheTablesThatExcludeSlackExcludeChannelToo:
    """These three list ``TELEGRAM, WEBCHAT``. Slack is not in them, and neither
    is a plugin channel: both hold a shared session rather than a per-person
    one, and neither writes a ``trigger_detail`` the person-linkage resolver can
    parse. Recorded as a decision so it stops being an omission."""

    def _members(self, dotted: str, function: str) -> set[str]:
        import ast

        from robothor.engine.tests.astcheck import function_def, module_tree  # noqa: F401

        branch = function_def(dotted, function)
        found: set[str] = set()
        for node in ast.walk(branch):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "TriggerType"
            ):
                found.add(node.attr)
        return found

    def test_the_warmup_table_treats_them_alike(self):
        members = self._members("robothor.engine.runner", "execute")
        assert ("SLACK" in members) == ("CHANNEL" in members), (
            "the runner's trigger tables now treat a plugin channel and Slack "
            "differently; that may be right, but it has to be deliberate"
        )

    def test_the_verification_skip_treats_them_alike(self):
        members = self._members("robothor.engine.run_lifecycle", "_should_verify")
        assert ("SLACK" in members) == ("CHANNEL" in members)


class TestTheDatabaseAcceptsIt:
    def test_the_check_constraint_lists_every_member(self):
        """Already covered by ``test_schema_drift``; asserted here too because
        this file is where somebody adding the next member will look."""
        from robothor.engine.tests.test_schema_drift import _newest_check_values

        allowed, _source = _newest_check_values("agent_runs", "trigger_type")
        assert TriggerType.CHANNEL.value in allowed
