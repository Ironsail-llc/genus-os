"""Tests for agent review DAL functions."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _make_mock_conn(fetchone_return=None, fetchall_return=None, rowcount=1):
    """Build a mock connection + cursor for DAL tests."""
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fetchone_return
    mock_cur.fetchall.return_value = fetchall_return or []
    mock_cur.rowcount = rowcount

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestCreateReview:
    @patch("robothor.crm.dal.get_connection")
    def test_create_review_returns_uuid(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_review

        review_id = create_review(
            agent_id="email-classifier",
            reviewer="operator",
            reviewer_type="operator",
            rating=4,
            feedback="Good classification accuracy",
        )

        assert review_id is not None
        assert mock_cur.execute.called
        sql = mock_cur.execute.call_args[0][0]
        assert "agent_reviews" in sql
        assert "INSERT" in sql.upper()

    @patch("robothor.crm.dal.get_connection")
    def test_create_review_with_categories(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_review

        review_id = create_review(
            agent_id="main",
            reviewer="operator",
            reviewer_type="operator",
            rating=5,
            categories={"accuracy": 5, "speed": 4, "tone": 5},
            feedback="Excellent response quality",
            action_items=["Keep current prompt style"],
        )

        assert review_id is not None
        params = mock_cur.execute.call_args[0][1]
        # categories should be JSON-encoded
        assert any(isinstance(p, str) and "accuracy" in p for p in params if p)

    @patch("robothor.crm.dal.get_connection")
    def test_create_review_with_run_id(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_review

        review_id = create_review(
            agent_id="email-responder",
            reviewer="buddy",
            reviewer_type="system",
            rating=2,
            run_id="run-abc-123",
            feedback="Overall score dropped to 48",
        )

        assert review_id is not None

    @patch("robothor.crm.dal.get_connection")
    def test_create_review_validates_rating_bounds(self, mock_get_conn):
        """Rating must be 1-5. DAL should clamp or reject."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_review

        # Rating 0 should be clamped to 1
        review_id = create_review(
            agent_id="test",
            reviewer="test",
            reviewer_type="system",
            rating=0,
        )
        assert review_id is not None
        params = mock_cur.execute.call_args[0][1]
        # Find the rating param (should be clamped to 1)
        assert 1 in params


class TestGetReviews:
    @patch("robothor.crm.dal.get_connection")
    def test_get_reviews_returns_list(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(
            fetchall_return=[
                {
                    "id": "review-1",
                    "agent_id": "email-classifier",
                    "reviewer": "operator",
                    "reviewer_type": "operator",
                    "rating": 4,
                    "categories": json.dumps({"accuracy": 4}),
                    "feedback": "Good work",
                    "action_items": None,
                    "run_id": None,
                    "created_at": "2026-04-15T10:00:00",
                }
            ]
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_reviews

        reviews = get_reviews("email-classifier", days=30)
        assert len(reviews) == 1
        assert reviews[0]["rating"] == 4
        assert reviews[0]["reviewer"] == "operator"

    @patch("robothor.crm.dal.get_connection")
    def test_get_reviews_empty(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_reviews

        reviews = get_reviews("new-agent", days=30)
        assert reviews == []

    @patch("robothor.crm.dal.get_connection")
    def test_get_reviews_filters_by_reviewer_type(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_reviews

        get_reviews("email-classifier", days=30, reviewer_type="operator")
        sql = mock_cur.execute.call_args[0][0]
        assert "reviewer_type" in sql


class TestGetReviewSummary:
    @patch("robothor.crm.dal.get_connection")
    def test_summary_with_reviews(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        # First query: aggregate stats
        # Second query: recent feedback
        mock_cur.fetchone.return_value = {
            "count": 5,
            "avg_rating": 3.8,
        }
        mock_cur.fetchall.return_value = [
            {"feedback": "Good accuracy", "rating": 4, "reviewer": "operator"},
            {"feedback": "Slow responses", "rating": 3, "reviewer": "buddy"},
        ]
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_review_summary

        summary = get_review_summary("email-classifier", days=30)
        assert summary["count"] == 5
        assert summary["avg_rating"] == 3.8
        assert len(summary["recent_feedback"]) == 2

    @patch("robothor.crm.dal.get_connection")
    def test_summary_no_reviews(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.return_value = {"count": 0, "avg_rating": None}
        mock_cur.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_review_summary

        summary = get_review_summary("new-agent")
        assert summary["count"] == 0
        assert summary["avg_rating"] is None
