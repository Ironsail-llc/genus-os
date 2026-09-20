"""When the accidental-paste backstop applies, and when it must not.

``robothor.autonomy.intake.protect_payment_text`` masks Luhn-valid 12-19
digit runs and appends an advisory sentence addressed to the operator:

    [Payment details were withheld from chat. Enroll securely at
    /account/autonomy.]

Its own docstring says to call it on text a PERSON just typed, and not "on
log lines, page content or anything else arriving from outside". Three call
sites did not honour that. ``AgentRunner.execute`` is the single entry point
for every trigger type, so ``WEBHOOK``, ``FEDERATION``, ``EVENT``, ``CRON``
and ``SUB_AGENT`` all ran through it, and ``AgentSession.start`` ran it again
on every run of any kind. A probe of eight strings came back with six
rewritten, including an IMEI and two tracking numbers.

Two things are wrong with that, and the second is the serious one:

* Luhn alone cannot tell a card from an IMEI (every IMEI is Luhn-valid by
  construction), a consignment number or an order id, so text nobody typed
  lost identifiers an agent needed.
* The appended sentence is ``[...]``-shaped, the platform's own shape for a
  system note. A federated peer's message or a webhook body containing any
  long Luhn-valid number therefore injected a pseudo-system line into the
  model's context, chosen by whoever sent it.

So the policy lives here, next to the trigger types it reads, and both call
sites ask this module rather than deciding for themselves.
"""

from __future__ import annotations

from robothor.engine.models import TriggerType

#: Trigger types whose message text a PERSON typed.
#:
#: Deliberately NOT ``agent_priority._INTERACTIVE_TRIGGERS``, which answers a
#: different question — "is a human waiting?" — and therefore includes
#: ``CHANNEL_EVENT``, the main agent waking after the fleet surfaced something
#: to a channel. A human is waiting there; the words are an agent's. Sharing
#: one set would have quietly put the advisory back on machine-authored text.
#:
#: Everything absent from this set is text that arrived from somewhere: a
#: webhook body, a federated peer, an event payload, a cron prompt, a parent
#: agent's instruction to its child.
HUMAN_AUTHORED_TRIGGERS: frozenset[TriggerType] = frozenset(
    {
        TriggerType.TELEGRAM,
        TriggerType.WEBCHAT,
        TriggerType.SLACK,
        # Every plugin channel that receives, as one member — a Teams or
        # Discord message is a person typing exactly as a Slack one is.
        TriggerType.CHANNEL,
        TriggerType.IDE,
        # The CLI: `genus engine run <agent> "<text>"`, the operator typing.
        TriggerType.MANUAL,
    }
)


def protect_if_human_chat(message: str, trigger_type: TriggerType | None) -> str:
    """``message``, with the payment backstop applied only if a person typed it.

    Fails CLOSED on the injection rather than on the courtesy: an unknown or
    missing trigger is treated as machine-authored and passed through. The
    tempting default is the other way round — "when in doubt, mask" — and
    that is precisely how the advisory ended up on federation traffic.
    Missing the backstop costs one operator one mask on a path that should
    not carry a card at all; applying it wrongly hands an attacker a line in
    the model's context.
    """
    if trigger_type not in HUMAN_AUTHORED_TRIGGERS:
        return message
    from robothor.autonomy.intake import protect_payment_text

    return protect_payment_text(message)
