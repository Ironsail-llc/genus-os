"""The payment backstop belongs to a person typing, and to nothing else.

``protect_payment_text`` masks Luhn-valid 12-19 digit runs and appends

    [Payment details were withheld from chat. Enroll securely at
    /account/autonomy.]

That sentence is addressed to the operator, and it is ``[...]``-shaped, which
is how this platform writes system notes to the model. Applied to text a
PERSON typed it is a courtesy. Applied to text that ARRIVED FROM OUTSIDE it is
an injection primitive: a federated peer's message or a webhook body only has
to contain any Luhn-valid long number — a tracking number, an order id, an
IMEI, all Luhn-valid — to get a pseudo-system line into the model's context.

It was being applied in ``AgentRunner.execute``, the single entry point for
every trigger type, so ``WEBHOOK``, ``FEDERATION``, ``EVENT``, ``CRON`` and
``SUB_AGENT`` all went through it; and again in ``AgentSession.start``, which
every run reaches. A probe of eight strings came back with six rewritten.

``robothor.autonomy.intake.protect_payment_text``'s own docstring says "do not
call it on log lines, page content or anything else arriving from outside".
``execute(message=...)`` on a webhook run is exactly that. This file pins the
gate that makes the code obey the contract.
"""

from __future__ import annotations

import pytest

from robothor.engine.chat_backstop import HUMAN_AUTHORED_TRIGGERS, protect_if_human_chat
from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession

#: Luhn-valid, 16 digits, and a real Visa test PAN — the exact thing the
#: backstop exists to catch, so a trigger type that lets it through is
#: letting through the case that matters.
PAN = "4111111111111111"

#: Luhn-valid and 15 digits, but an IMEI. Every IMEI is Luhn-valid by
#: construction, which is why "Luhn-valid" cannot mean "a card" on text
#: nobody typed.
IMEI = "490154203237518"

ADVISORY = "/account/autonomy"


class TestWhoTypedIt:
    """Trigger types are split by ONE question: did a person type this text?

    Not "is a person waiting" — that is `agent_priority._INTERACTIVE_TRIGGERS`,
    which also holds ``CHANNEL_EVENT`` (the main agent waking after the fleet
    surfaced something to a channel). A human is waiting there; no human typed
    the words.
    """

    @pytest.mark.parametrize(
        "trigger",
        [
            TriggerType.TELEGRAM,
            TriggerType.WEBCHAT,
            TriggerType.SLACK,
            TriggerType.CHANNEL,
            TriggerType.IDE,
            TriggerType.MANUAL,
        ],
    )
    def test_a_person_typing_still_gets_the_backstop(self, trigger):
        out = protect_if_human_chat(f"use my card {PAN}", trigger)
        assert PAN not in out
        assert ADVISORY in out

    @pytest.mark.parametrize(
        "trigger",
        [
            TriggerType.WEBHOOK,
            TriggerType.FEDERATION,
            TriggerType.EVENT,
            TriggerType.CRON,
            TriggerType.HOOK,
            TriggerType.SUB_AGENT,
            TriggerType.WORKFLOW,
            TriggerType.CHANNEL_EVENT,
        ],
    )
    def test_text_from_outside_is_passed_through_untouched(self, trigger):
        text = f"peer reports order {PAN} shipped; imei {IMEI}"
        assert protect_if_human_chat(text, trigger) == text

    def test_an_unknown_or_missing_trigger_is_not_treated_as_chat(self):
        """Fail CLOSED on the injection, not on the courtesy.

        Getting this wrong in the safe-looking direction — "when in doubt,
        mask" — is what put the advisory on federation traffic.
        """
        assert protect_if_human_chat(f"order {PAN}", None) == f"order {PAN}"

    def test_the_two_trigger_sets_are_stated_once(self):
        assert set(TriggerType) >= HUMAN_AUTHORED_TRIGGERS
        assert TriggerType.FEDERATION not in HUMAN_AUTHORED_TRIGGERS
        assert TriggerType.WEBHOOK not in HUMAN_AUTHORED_TRIGGERS
        assert TriggerType.SUB_AGENT not in HUMAN_AUTHORED_TRIGGERS
        assert TriggerType.CHANNEL_EVENT not in HUMAN_AUTHORED_TRIGGERS
        assert TriggerType.TELEGRAM in HUMAN_AUTHORED_TRIGGERS


class TestTheSessionObeysItsOwnRunsTrigger:
    """``AgentSession.start`` applied it to every run regardless."""

    def test_a_federation_run_keeps_its_message_verbatim(self):
        session = AgentSession("test-agent", TriggerType.FEDERATION)
        text = f"remote peer says consignment {PAN} cleared customs"
        session.start("System prompt", text, [])
        assert session.messages[1]["content"] == text
        assert "[" not in session.messages[1]["content"]

    def test_a_webhook_run_keeps_its_body_verbatim(self):
        session = AgentSession("test-agent", TriggerType.WEBHOOK)
        text = f'{{"reference": "{PAN}"}}'
        session.start("System prompt", text, [])
        assert session.messages[1]["content"] == text

    def test_a_webchat_run_still_has_a_card_withheld(self):
        session = AgentSession("test-agent", TriggerType.WEBCHAT)
        session.start("System prompt", f"here is my card {PAN}", [])
        content = session.messages[1]["content"]
        assert PAN not in content
        assert ADVISORY in content

    def test_the_stored_originating_message_is_the_protected_one(self):
        """What the deliverable contract reads must match what the model saw."""
        session = AgentSession("test-agent", TriggerType.WEBCHAT)
        session.start("System prompt", f"card {PAN}", [])
        assert session.originating_message == session.messages[1]["content"]
