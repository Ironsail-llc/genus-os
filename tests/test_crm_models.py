"""Tests for robothor.crm.models — response shape converters (pure unit tests)."""

from datetime import UTC, datetime

from robothor.crm.models import (
    company_to_dict,
    conversation_to_dict,
    note_to_dict,
    person_to_dict,
    task_to_dict,
)

NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


class TestPersonToDict:
    def test_full_record(self):
        row = {
            "id": "abc-123",
            "first_name": "Philip",
            "last_name": "Ironsail",
            "email": "philip@example.com",
            "phone": "+15551234567",
            "job_title": "CEO",
            "city": "New York",
            "avatar_url": "https://example.com/photo.jpg",
            "linkedin_url": "https://linkedin.com/in/philip",
            "additional_emails": ["alt@example.com"],
            "additional_phones": ["+15559876543"],
            "company_id": "comp-456",
            "company_name": "Ironsail LLC",
            "updated_at": NOW,
            "created_at": NOW,
        }
        result = person_to_dict(row)
        assert result["id"] == "abc-123"
        assert result["name"]["firstName"] == "Philip"
        assert result["name"]["lastName"] == "Ironsail"
        assert result["emails"]["primaryEmail"] == "philip@example.com"
        assert result["phones"]["primaryPhoneNumber"] == "+15551234567"
        assert result["jobTitle"] == "CEO"
        assert result["company"]["name"] == "Ironsail LLC"
        assert result["additionalEmails"] == ["alt@example.com"]

    def test_minimal_record(self):
        row = {
            "id": "abc",
            "first_name": None,
            "last_name": None,
            "updated_at": None,
            "created_at": None,
        }
        result = person_to_dict(row)
        assert result["name"]["firstName"] == ""
        assert result["name"]["lastName"] == ""
        assert result["company"] is None
        assert result["additionalEmails"] == []

    def test_id_is_string(self):
        row = {"id": "uuid-here", "updated_at": None, "created_at": None}
        result = person_to_dict(row)
        assert isinstance(result["id"], str)


class TestCompanyToDict:
    def test_full_record(self):
        row = {
            "id": "comp-1",
            "name": "Ironsail LLC",
            "domain_name": "ironsail.ai",
            "employees": 5,
            "address": "123 Main St",
            "linkedin_url": "https://linkedin.com/company/ironsail",
            "ideal_customer_profile": True,
            "updated_at": NOW,
            "created_at": NOW,
        }
        result = company_to_dict(row)
        assert result["name"] == "Ironsail LLC"
        assert result["domainName"] == "ironsail.ai"
        assert result["employees"] == 5
        assert result["idealCustomerProfile"] is True

    def test_minimal_record(self):
        row = {"id": "x", "updated_at": None, "created_at": None}
        result = company_to_dict(row)
        assert result["name"] == ""
        assert result["employees"] is None


class TestNoteToDict:
    def test_with_links(self):
        row = {
            "id": "note-1",
            "title": "Meeting notes",
            "body": "Discussed Q3 plans",
            "person_id": "person-1",
            "company_id": "comp-1",
            "updated_at": NOW,
            "created_at": NOW,
        }
        result = note_to_dict(row)
        assert result["title"] == "Meeting notes"
        assert result["personId"] == "person-1"
        assert result["companyId"] == "comp-1"

    def test_no_links(self):
        row = {
            "id": "n",
            "title": "T",
            "body": "B",
            "person_id": None,
            "company_id": None,
            "updated_at": None,
            "created_at": None,
        }
        result = note_to_dict(row)
        assert result["personId"] is None
        assert result["companyId"] is None


class TestTaskToDict:
    def test_full_task(self):
        row = {
            "id": "task-1",
            "title": "Follow up",
            "body": "Send proposal",
            "status": "IN_PROGRESS",
            "due_at": NOW,
            "person_id": "p-1",
            "company_id": None,
            "updated_at": NOW,
            "created_at": NOW,
        }
        result = task_to_dict(row)
        assert result["status"] == "IN_PROGRESS"
        assert result["dueAt"] is not None

    def test_defaults(self):
        row = {"id": "t", "updated_at": None, "created_at": None}
        result = task_to_dict(row)
        assert result["status"] == "TODO"
        assert result["dueAt"] is None


class TestConversationToDict:
    def test_full_conversation(self):
        row = {
            "id": 42,
            "status": "open",
            "inbox_name": "Email",
            "messages_count": 5,
            "person_id": "p-1",
            "person_name": "Philip Ironsail",
            "metadata": {"source": "email"},
            "last_activity_at": NOW,
            "updated_at": NOW,
            "created_at": NOW,
        }
        result = conversation_to_dict(row)
        assert result["id"] == 42  # Integer, not UUID
        assert result["status"] == "open"
        assert result["messagesCount"] == 5
        assert result["personName"] == "Philip Ironsail"

    def test_minimal(self):
        row = {"id": 1, "updated_at": None, "created_at": None, "last_activity_at": None}
        result = conversation_to_dict(row)
        assert result["status"] == "open"
        assert result["messagesCount"] == 0
