from robothor.autonomy.intake import marker_truncated, protect_payment_text


def test_card_paste_never_becomes_model_input():
    value = protect_payment_text("Use my card 4242 4242 4242 4242 cvv: 123")
    assert "4242" not in value
    assert "123" not in value
    assert "/account/autonomy" in value


def test_ordinary_text_and_timestamps_remain_readable():
    text = "Create an account tomorrow; reference 20260919143000."
    assert protect_payment_text(text) == text


def test_redaction_is_idempotent():
    once = protect_payment_text("4242424242424242")
    assert protect_payment_text(once) == once


def test_a_multi_line_message_is_not_silently_docked_of_its_tail():
    """`intercept` only consumes a message that STARTS with /secure, but the
    backstop's marker is `(?im)^\\s*/secure`, which matches at any line start.
    A perfectly ordinary message therefore lost everything from that line on,
    with nothing to tell the operator a tail had been removed."""
    text = "Draft the checklist:\n/secure the loading bay doors\nthen email it to the team"
    protected = protect_payment_text(text)

    # The cut itself is the backstop and stays — but the operator has to be
    # able to tell that it happened, and to what.
    assert marker_truncated(text), "the truncation is not reportable"
    assert not marker_truncated("Draft the checklist:\nthen email it to the team")
    assert not marker_truncated("/secure profile")
    assert "loading bay" not in protected
