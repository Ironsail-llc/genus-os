"""Actual hardened subprocess + sandboxed Chromium + disposable PostgreSQL.

The child intercepts all website traffic with a controlled fixture; no account
or payment request leaves the machine. These require AUTONOMY_TEST_DSN and a
sandbox-capable installed Chromium (or the operator's executable override).
"""

import base64
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from psycopg2.extensions import parse_dsn

from robothor.autonomy.models import Delegation, ResourceInput, WebOperation


@pytest.mark.timeout(60)
def test_real_worker_returns_only_references_after_account_creation(store, identity):
    credential = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Website login",
            origin="https://shop.example",
            payload=json.dumps(
                {"username": "alice@example.com", "password": "private-process-test"}
            ),
        ),
    )
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://shop.example"},
            actions={"account"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    operation = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://shop.example",
            action="account",
            purpose="Synthetic application",
            idempotency_key="subprocess-account",
        ),
    )
    plan = {
        "url": "https://shop.example/signup",
        "submit_selector": "#submit",
        "success_selector": "#confirmation",
        "success_text": "Account created",
        "fields": [
            {
                "selector": "#password",
                "resource_id": credential["id"],
                "kind": "credential",
                "field": "password",
            }
        ],
    }
    payload = {
        "scope": identity.model_dump(),
        "operation_id": operation["id"],
        "agent_id": "main",
        "database": parse_dsn(os.environ["AUTONOMY_TEST_DSN"]),
        "keys": {"v1": base64.b64encode(b"x" * 32).decode()},
        "key_id": "v1",
        "plan": plan,
    }
    # Test-only route substitution, before worker.main hardens the process.
    script = '''
from robothor.autonomy import worker
async def merchant(route):
    if route.request.url.endswith('/submit'):
        assert route.request.post_data == 'private-process-test'
        await route.fulfill(body='ok')
    else:
        await route.fulfill(content_type='text/html', body="""<form><input id='password' required>
<button id='submit'>Create account</button></form><div id='confirmation' hidden></div>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();
await fetch('/submit',{method:'POST',body:document.querySelector('#password').value});
document.querySelector('#confirmation').hidden=false;
document.querySelector('#confirmation').textContent='Account created';};</script>""")
worker.public_request = merchant
worker.main()
'''
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "PLAYWRIGHT_BROWSERS_PATH",
            "ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE",
        )
        if key in os.environ
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=45,
        env=env,
    )
    assert result.returncode == 0
    assert "private-process-test" not in result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["state"] == "completed", output
    assert output["session_resource_id"]
    assert store.operation(identity, operation["id"])["state"] == "completed"
