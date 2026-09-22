"""Real loopback HTTP admission with a scripted provider in the private daemon."""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

MANIFEST = """id: main
name: Synthetic admission
model:
  primary: openrouter/test/model
  fallbacks: []
schedule:
  enabled: false
  timeout_seconds: 30
delivery:
  mode: none
tools_allowed: []
task_protocol: false
notification_inbox: false
shared_working_state: false
bootstrap_files: []
v2:
  difficulty_class: simple
"""
REPLY = "Synthetic HTTP request acknowledged."


def install():
    """Install only within the network-guarded disposable daemon process."""
    import litellm

    from robothor.engine.llm_client import LLMClient
    from robothor.engine.tools.registry import ToolRegistry

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        assert models == ["openrouter/test/model"]
        with Path(os.environ["RUNTIME_DRILL_ROOT"], "chat-provider-calls.jsonl").open("a") as log:
            log.write(json.dumps({"reply": REPLY}) + "\n")
        return litellm.ModelResponse(
            model=models[0],
            choices=[{"message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def deny_tools(*args, **kwargs):
        raise AssertionError("HTTP admission fixture must not execute a business tool")

    LLMClient._call_llm = provider
    LLMClient._call_llm_streaming = provider
    ToolRegistry.execute = deny_tools


def exercise(port, root):
    base = f"http://127.0.0.1:{port}"
    session, request = "agent:main:rollback-http-" + uuid4().hex, str(uuid4())
    payload = {
        "message": "Acknowledge this request.",
        "session_key": session,
        "request_id": request,
    }
    with urlopen(
        Request(
            base + "/chat/send",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=20,
    ) as response:
        assert response.status == 200
        stream = response.read().decode()
    done = [
        json.loads(part.split("data: ", 1)[1])
        for part in stream.split("\n\n")
        if part.startswith("event: done\n")
    ]
    assert len(done) == 1 and done[0]["status"] == "completed", stream
    assert done[0]["text"] == REPLY, done
    query = urlencode({"session_key": session, "request_id": request})
    expires = time.monotonic() + 5
    while True:
        with urlopen(base + "/chat/outcome?" + query, timeout=2) as response:
            outcome = json.load(response)
        if outcome.get("terminal"):
            break
        assert time.monotonic() < expires, outcome
        time.sleep(0.05)
    assert outcome["text"].startswith(REPLY), outcome
    calls = (root / "chat-provider-calls.jsonl").read_text().splitlines()
    assert len(calls) == 1, calls
    return {"completed": True, "audit_recovered": True, "provider_calls": 1}
