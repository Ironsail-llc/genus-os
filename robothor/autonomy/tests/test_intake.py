from robothor.autonomy.intake import protect_payment_text


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
