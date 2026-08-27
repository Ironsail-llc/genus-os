"""The tool surface speaks two languages; a model should not have to guess.

Measured across the 172 tools that take parameters:

    snake_case only        82
    camelCase only         26
    MIXED in one tool       1   (append_to_block: maxEntries + block_name)
    single-word only       63

So 82 tools want `task_id` and 26 want `toAgent`. A model that has just called
get_task({"id": ...}) and create_task({"title": ...}) will send `to_agent` to
send_notification — which is exactly what happened when this was found, on the
first unprompted attempt while reading the schema.

What came back was:

    {"error": "Failed to send notification"}

The handler did not raise; it used defaults, hit a database CHECK constraint,
and returned a generic string. Nothing named the wrong argument, so the model
has nothing to correct toward and retries the same shape.

Normalising here fixes every tool at once and changes no schema: an argument is
matched to a declared parameter by ignoring case and underscores. A name that
already matches exactly always wins, and a genuinely ambiguous alias is left
alone rather than guessed at.
"""

from __future__ import annotations

from robothor.engine.tools.dispatch import normalise_arguments

SCHEMA = {"toAgent": {}, "notificationType": {}, "subject": {}, "body": {}}


def test_snake_case_reaches_a_camel_case_parameter():
    out = normalise_arguments({"to_agent": "main", "notification_type": "info"}, SCHEMA)
    assert out["toAgent"] == "main"
    assert out["notificationType"] == "info"
    assert "to_agent" not in out


def test_an_exact_match_is_untouched():
    out = normalise_arguments({"toAgent": "main", "subject": "hi"}, SCHEMA)
    assert out == {"toAgent": "main", "subject": "hi"}


def test_an_exact_match_wins_over_an_alias():
    """If both spellings arrive, the declared one is authoritative."""
    out = normalise_arguments({"toAgent": "right", "to_agent": "wrong"}, SCHEMA)
    assert out["toAgent"] == "right"


def test_an_unknown_argument_is_left_alone():
    """Not our business to invent a mapping — the handler may accept extras."""
    out = normalise_arguments({"mystery": 1}, SCHEMA)
    assert out == {"mystery": 1}


def test_an_ambiguous_alias_is_not_guessed():
    """Two parameters differing only by case must not silently collide."""
    schema = {"userId": {}, "user_id": {}}
    out = normalise_arguments({"userid": "x"}, schema)
    assert out == {"userid": "x"}, "an ambiguous alias was guessed at"


def test_no_schema_means_no_change():
    assert normalise_arguments({"a_b": 1}, {}) == {"a_b": 1}
    assert normalise_arguments({"a_b": 1}, None) == {"a_b": 1}


def test_camel_reaches_a_snake_case_parameter():
    """The mapping runs both ways — 82 tools are snake_case."""
    out = normalise_arguments({"taskId": "t1"}, {"task_id": {}})
    assert out["task_id"] == "t1"


def test_the_registry_actually_normalises():
    """An unwired helper is the defect class this whole session is about.

    Asserted against the registry's own source, so deleting the call fails here.
    """
    import inspect

    from robothor.engine.tools.registry import ToolRegistry

    src = inspect.getsource(ToolRegistry.execute)
    assert "normalise_arguments(" in src, "the registry never normalises arguments"
