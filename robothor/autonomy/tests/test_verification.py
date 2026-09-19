import base64
from datetime import UTC, datetime
from email.utils import format_datetime

from robothor.autonomy.verification import extract_verification


def email():
    now = datetime.now(UTC)
    return {
        "internalDate": str(int(now.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "login@shop.example"},
                {"name": "To", "value": "alice@example.com"},
                {"name": "Date", "value": format_datetime(now)},
                {
                    "name": "Authentication-Results",
                    "value": "mx.google.com; dmarc=pass header.from=shop.example;",
                },
            ],
            "body": {
                "data": base64.urlsafe_b64encode(b"Your verification code is 123456").decode()
            },
        },
    }


def read(message):
    return extract_verification(
        message,
        recipient="alice@example.com",
        destination="https://shop.example",
        after=int(datetime.now(UTC).timestamp()) - 60,
        mode="code",
    )


def test_fresh_authenticated_verification_is_extracted():
    assert read(email()) == "123456"


def test_wrong_sender_wrong_recipient_and_stale_message_are_rejected():
    for header, value in [
        ("From", "attacker@evil.example"),
        ("To", "bob@example.com"),
        ("Authentication-Results", "dmarc=fail header.from=shop.example;"),
    ]:
        message = email()
        for item in message["payload"]["headers"]:
            if item["name"] == header:
                item["value"] = value
        assert read(message) is None
    message = email()
    message["internalDate"] = "1"
    assert read(message) is None


def test_untrusted_or_duplicate_authentication_headers_are_rejected():
    message = email()
    message["payload"]["headers"][-1]["value"] = (
        "attacker.example; dmarc=pass header.from=shop.example;"
    )
    assert read(message) is None
    message = email()
    message["payload"]["headers"].append(
        {
            "name": "Authentication-Results",
            "value": "mx.google.com; dmarc=pass header.from=shop.example;",
        }
    )
    assert read(message) is None
