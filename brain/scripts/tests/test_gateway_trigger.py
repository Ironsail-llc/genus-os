#!/usr/bin/env python3
"""Tests for gateway_trigger.py — WebSocket gateway client."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gateway_trigger import (
    PROTOCOL_VERSION,
    GatewayAuthError,
    GatewayConnectionError,
    _handshake,
    _read_job_state,
    trigger_and_wait,
    trigger_job,
    wait_for_job_completion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _challenge_frame() -> str:
    return json.dumps(
        {
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": "test-nonce-123", "ts": 1700000000000},
        }
    )


def _hello_ok_frame(connect_id: str) -> str:
    return json.dumps(
        {
            "type": "res",
            "id": connect_id,
            "ok": True,
            "payload": {"type": "hello-ok", "protocol": PROTOCOL_VERSION},
        }
    )


def _auth_failure_frame(connect_id: str) -> str:
    return json.dumps(
        {
            "type": "res",
            "id": connect_id,
            "ok": False,
            "error": {"code": "auth_failed", "message": "Invalid token"},
        }
    )


def _cron_run_ok(req_id: str, ran: bool = True, reason: str = None) -> str:
    payload = {"ran": ran}
    if reason:
        payload["reason"] = reason
    return json.dumps(
        {
            "type": "res",
            "id": req_id,
            "ok": True,
            "payload": payload,
        }
    )


def _cron_run_error(req_id: str, message: str = "job not found") -> str:
    return json.dumps(
        {
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {"code": "not_found", "message": message},
        }
    )


def _mock_ws_context(recv_sequence: list):
    """Create a mock WebSocket context manager that yields frames in sequence."""
    ws = MagicMock()
    ws.recv = MagicMock(side_effect=recv_sequence)
    sent_messages = []
    ws.send = MagicMock(side_effect=lambda msg: sent_messages.append(json.loads(msg)))
    ws._sent = sent_messages

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ws)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, ws


# ---------------------------------------------------------------------------
# Tests: _handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    def test_successful_handshake(self):
        ws = MagicMock()
        # First recv: challenge, second recv: hello-ok
        # We need to capture the connect_id from the sent message
        sent = []
        ws.send = MagicMock(side_effect=lambda msg: sent.append(json.loads(msg)))

        def recv_side_effect(timeout=None):
            if len(sent) == 0:
                return _challenge_frame()
            else:
                connect_id = sent[0]["id"]
                return _hello_ok_frame(connect_id)

        ws.recv = MagicMock(side_effect=recv_side_effect)

        result = _handshake(ws)
        assert result["type"] == "hello-ok"
        assert result["protocol"] == PROTOCOL_VERSION

        # Verify connect params
        connect_req = sent[0]
        assert connect_req["method"] == "connect"
        assert connect_req["params"]["auth"]["token"] == "test-token"
        assert connect_req["params"]["minProtocol"] == 3
        assert connect_req["params"]["maxProtocol"] == 3
        assert connect_req["params"]["client"]["mode"] == "ui"

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "bad-token"})
    def test_auth_failure_raises(self):
        ws = MagicMock()
        sent = []
        ws.send = MagicMock(side_effect=lambda msg: sent.append(json.loads(msg)))

        def recv_side_effect(timeout=None):
            if len(sent) == 0:
                return _challenge_frame()
            else:
                return _auth_failure_frame(sent[0]["id"])

        ws.recv = MagicMock(side_effect=recv_side_effect)

        with pytest.raises(GatewayAuthError, match="Invalid token"):
            _handshake(ws)

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    def test_unexpected_first_frame_raises(self):
        ws = MagicMock()
        ws.recv = MagicMock(
            return_value=json.dumps(
                {
                    "type": "event",
                    "event": "tick",
                    "payload": {},
                }
            )
        )

        with pytest.raises(GatewayConnectionError, match="Expected connect.challenge"):
            _handshake(ws)


# ---------------------------------------------------------------------------
# Tests: trigger_job
# ---------------------------------------------------------------------------


class TestTriggerJob:
    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    @patch("gateway_trigger.ws_client.connect")
    def test_trigger_success(self, mock_connect):
        sent = []
        ws = MagicMock()
        ws.send = MagicMock(side_effect=lambda msg: sent.append(json.loads(msg)))

        call_count = [0]

        def recv_side_effect(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _challenge_frame()
            elif call_count[0] == 2:
                return _hello_ok_frame(sent[0]["id"])
            else:
                # cron.run response
                return _cron_run_ok(sent[1]["id"])

        ws.recv = MagicMock(side_effect=recv_side_effect)
        mock_connect.return_value.__enter__ = MagicMock(return_value=ws)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = trigger_job("email-classifier-0001")
        assert result == {"ok": True, "ran": True, "reason": None}

        # Verify cron.run request
        cron_req = sent[1]
        assert cron_req["method"] == "cron.run"
        assert cron_req["params"]["id"] == "email-classifier-0001"
        assert cron_req["params"]["mode"] == "force"

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    @patch("gateway_trigger.ws_client.connect")
    def test_trigger_not_ran(self, mock_connect):
        sent = []
        ws = MagicMock()
        ws.send = MagicMock(side_effect=lambda msg: sent.append(json.loads(msg)))

        call_count = [0]

        def recv_side_effect(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _challenge_frame()
            elif call_count[0] == 2:
                return _hello_ok_frame(sent[0]["id"])
            else:
                return _cron_run_ok(sent[1]["id"], ran=False, reason="not-due")

        ws.recv = MagicMock(side_effect=recv_side_effect)
        mock_connect.return_value.__enter__ = MagicMock(return_value=ws)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = trigger_job("email-classifier-0001", mode="due")
        assert result == {"ok": True, "ran": False, "reason": "not-due"}

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    @patch("gateway_trigger.ws_client.connect")
    def test_trigger_error(self, mock_connect):
        sent = []
        ws = MagicMock()
        ws.send = MagicMock(side_effect=lambda msg: sent.append(json.loads(msg)))

        call_count = [0]

        def recv_side_effect(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _challenge_frame()
            elif call_count[0] == 2:
                return _hello_ok_frame(sent[0]["id"])
            else:
                return _cron_run_error(sent[1]["id"])

        ws.recv = MagicMock(side_effect=recv_side_effect)
        mock_connect.return_value.__enter__ = MagicMock(return_value=ws)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = trigger_job("nonexistent-job")
        assert result["ok"] is False
        assert result["ran"] is False
        assert "not found" in result["reason"]

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    @patch("gateway_trigger.ws_client.connect")
    def test_connection_timeout_raises(self, mock_connect):
        mock_connect.side_effect = TimeoutError("Connection timed out")

        with pytest.raises(GatewayConnectionError, match="Connection failed"):
            trigger_job("test-job")

    @patch.dict("os.environ", {"OPENCLAW_GATEWAY_TOKEN": "test-token"})
    @patch("gateway_trigger.ws_client.connect")
    def test_os_error_raises(self, mock_connect):
        mock_connect.side_effect = OSError("Connection refused")

        with pytest.raises(GatewayConnectionError, match="Connection failed"):
            trigger_job("test-job")

    def test_missing_token_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(GatewayConnectionError, match="OPENCLAW_GATEWAY_TOKEN not set"):
                trigger_job("test-job")


# ---------------------------------------------------------------------------
# Tests: _read_job_state
# ---------------------------------------------------------------------------


class TestReadJobState:
    def test_reads_job_state(self, tmp_path):
        jobs_json = tmp_path / "jobs.json"
        jobs_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "id": "test-job",
                            "state": {
                                "lastRunAtMs": 1700000000000,
                                "lastStatus": "ok",
                                "lastDurationMs": 5000,
                            },
                        }
                    ],
                }
            )
        )

        with patch("gateway_trigger.JOBS_JSON", jobs_json):
            state = _read_job_state("test-job")

        assert state["lastRunAtMs"] == 1700000000000
        assert state["lastStatus"] == "ok"

    def test_returns_none_for_missing_job(self, tmp_path):
        jobs_json = tmp_path / "jobs.json"
        jobs_json.write_text(json.dumps({"version": 1, "jobs": []}))

        with patch("gateway_trigger.JOBS_JSON", jobs_json):
            assert _read_job_state("nonexistent") is None

    def test_returns_none_for_missing_file(self, tmp_path):
        with patch("gateway_trigger.JOBS_JSON", tmp_path / "nope.json"):
            assert _read_job_state("test") is None


# ---------------------------------------------------------------------------
# Tests: wait_for_job_completion
# ---------------------------------------------------------------------------


class TestWaitForJobCompletion:
    def test_detects_completion(self, tmp_path):
        jobs_json = tmp_path / "jobs.json"
        started_at = 1700000000000

        # Job completed after started_at
        jobs_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "id": "test-job",
                            "state": {
                                "lastRunAtMs": started_at + 5000,
                                "lastStatus": "ok",
                                "lastDurationMs": 5000,
                            },
                        }
                    ],
                }
            )
        )

        with patch("gateway_trigger.JOBS_JSON", jobs_json):
            result = wait_for_job_completion("test-job", started_at, poll_interval=0.01, max_wait=1)

        assert result["completed"] is True
        assert result["status"] == "ok"
        assert result["duration_ms"] == 5000

    def test_returns_false_on_timeout(self, tmp_path):
        jobs_json = tmp_path / "jobs.json"
        started_at = 1700000000000

        # Job hasn't run yet (lastRunAtMs < started_at)
        jobs_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "id": "test-job",
                            "state": {
                                "lastRunAtMs": started_at - 10000,
                                "lastStatus": "ok",
                                "runningAtMs": started_at + 100,
                            },
                        }
                    ],
                }
            )
        )

        with patch("gateway_trigger.JOBS_JSON", jobs_json):
            result = wait_for_job_completion(
                "test-job", started_at, poll_interval=0.01, max_wait=0.05
            )

        assert result["completed"] is False

    def test_waits_while_running(self, tmp_path):
        jobs_json = tmp_path / "jobs.json"
        started_at = 1700000000000

        # Start with job running, then update file to show completion
        jobs_json.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "id": "test-job",
                            "state": {
                                "runningAtMs": started_at + 100,
                                "lastRunAtMs": started_at - 5000,
                                "lastStatus": "ok",
                            },
                        }
                    ],
                }
            )
        )

        poll_count = [0]
        orig_read_job_state = _read_job_state

        def mock_read(job_id):
            poll_count[0] += 1
            if poll_count[0] <= 1:
                return {
                    "runningAtMs": started_at + 100,
                    "lastRunAtMs": started_at - 5000,
                    "lastStatus": "ok",
                }
            else:
                return {
                    "lastRunAtMs": started_at + 100,
                    "lastStatus": "ok",
                    "lastDurationMs": 30000,
                }

        with patch("gateway_trigger._read_job_state", side_effect=mock_read):
            result = wait_for_job_completion("test-job", started_at, poll_interval=0.01, max_wait=1)

        assert result["completed"] is True


# ---------------------------------------------------------------------------
# Tests: trigger_and_wait
# ---------------------------------------------------------------------------


class TestTriggerAndWait:
    @patch("gateway_trigger.wait_for_job_completion")
    @patch("gateway_trigger.trigger_job")
    def test_success_flow(self, mock_trigger, mock_wait):
        mock_trigger.return_value = {"ok": True, "ran": True, "reason": None}
        mock_wait.return_value = {"completed": True, "status": "ok", "duration_ms": 5000}

        result = trigger_and_wait("test-job", max_wait=60)

        assert result["triggered"] is True
        assert result["completed"] is True
        assert result["status"] == "ok"
        mock_trigger.assert_called_once_with("test-job")
        mock_wait.assert_called_once()

    @patch("gateway_trigger.trigger_job")
    def test_trigger_failure(self, mock_trigger):
        mock_trigger.return_value = {"ok": False, "ran": False, "reason": "connection refused"}

        result = trigger_and_wait("test-job")

        assert result["triggered"] is False
        assert result["completed"] is False
        assert result["reason"] == "connection refused"

    @patch("gateway_trigger.wait_for_job_completion")
    @patch("gateway_trigger.trigger_job")
    def test_already_running_waits_for_completion(self, mock_trigger, mock_wait):
        mock_trigger.return_value = {"ok": True, "ran": False, "reason": "already-running"}
        mock_wait.return_value = {"completed": True, "status": "ok", "duration_ms": 10000}

        result = trigger_and_wait("test-job")

        assert result["triggered"] is False
        assert result["completed"] is True
        assert result["status"] == "ok"
        assert result["reason"] is None
        mock_wait.assert_called_once()

    @patch("gateway_trigger.wait_for_job_completion")
    @patch("gateway_trigger.trigger_job")
    def test_already_running_timeout(self, mock_trigger, mock_wait):
        mock_trigger.return_value = {"ok": True, "ran": False, "reason": "already-running"}
        mock_wait.return_value = {"completed": False, "status": None, "duration_ms": None}

        result = trigger_and_wait("test-job")

        assert result["triggered"] is False
        assert result["completed"] is False
        assert result["reason"] == "already-running"

    @patch("gateway_trigger.trigger_job")
    def test_not_ran_other_reason(self, mock_trigger):
        mock_trigger.return_value = {"ok": True, "ran": False, "reason": "not-due"}

        result = trigger_and_wait("test-job")

        assert result["triggered"] is True
        assert result["completed"] is False
        assert result["reason"] == "not-due"
