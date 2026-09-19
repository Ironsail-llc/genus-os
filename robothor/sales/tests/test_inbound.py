"""Provider authentication and minimization precede durable storage."""

import json

import pytest

from robothor.sales.inbound import AuthenticationError, instantly_event


def test_instantly_custom_header_auth_and_workspace_binding():
    body = json.dumps(
        {
            "event_type": "reply_received",
            "workspace": "workspace-1",
            "campaign_id": "campaign-1",
            "timestamp": "2026-09-01T12:00:00Z",
            "lead_email": "alice@example.com",
            "email_account": "sales@example.com",
            "email_id": "message-1",
            "reply_text": "What are your terms?",
            "reply_subject": "Question",
            "extra_private_field": "discarded",
        }
    ).encode()
    result = instantly_event(body, "Bearer test-secret", "test-secret", "workspace-1")
    assert result["body"] == "What are your terms?"
    assert result["provider_id"] == "message-1"
    assert "extra_private_field" not in result
    with pytest.raises(AuthenticationError):
        instantly_event(body, "Bearer wrong", "test-secret", "workspace-1")
    with pytest.raises(AuthenticationError):
        instantly_event(body, "Bearer test-secret", "test-secret", "another-workspace")


def test_reply_addresses_are_inbound_not_outbound():
    raw = json.dumps(
        {
            "event_type": "reply_received",
            "workspace": "workspace-1",
            "timestamp": "2026-09-01T12:00:00Z",
            "lead_email": "alice@example.com",
            "email_account": "sales@example.com",
        }
    ).encode()
    result = instantly_event(raw, "Bearer test-secret", "test-secret", "workspace-1")
    assert result["sender"] == "alice@example.com"
    assert result["recipient"] == "sales@example.com"
    assert result["provider_id"] == ""  # Never invent a reply_to_uuid.
