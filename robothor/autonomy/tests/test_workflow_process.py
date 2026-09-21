"""Real service and separate controller processes preserve a browser wizard.

All merchant requests are intercepted by a fixture in the child. No account or
payment is sent to an external site; database access is restricted to *_test.
"""

import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
from psycopg2.extensions import parse_dsn

from robothor.autonomy.models import WebOperation
from robothor.autonomy.tests.test_workflow_store import prepared

SERVICE_SCRIPT = '''
import os
import psycopg2
from robothor.autonomy import worker
from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.workflows import service
dsn = os.environ["AUTONOMY_TEST_DSN"]
async def merchant(route):
    if route.request.url == "https://form.example/submit":
        assert route.request.post_data == "step-two"
        await route.fulfill(body="ok")
    else:
        await route.fulfill(content_type="text/html", body="""<form id="first"><input id="one"><button id="next">Next</button></form><form id="second" hidden><input id="two"><button id="submit">Apply</button></form><p></p>
<script>document.querySelector('#first').onsubmit=e=>{e.preventDefault();document.querySelector('#first').hidden=true;document.querySelector('#second').hidden=false;};document.querySelector('#second').onsubmit=async e=>{e.preventDefault();await fetch('/submit',{method:'POST',body:'step-two'});document.querySelector('p').textContent='Application received';};</script>""")
worker.public_request = merchant
service.AutonomyStore = lambda: AutonomyStore(lambda: psycopg2.connect(dsn), keys={"v1": b"x"*32}, key_id="v1")
service.main()
'''

CONTROLLER_SCRIPT = """
import asyncio, json, sys
from robothor.autonomy.models import Scope
from robothor.autonomy.workflows.client import invoke
data=json.load(sys.stdin)
print(json.dumps(asyncio.run(invoke(Scope.model_validate(data["scope"]), "main", data["request"]))))
"""


@pytest.mark.timeout(75)
def test_controller_restart_preserves_page_broker_restart_requires_reconciliation(
    store, identity, tmp_path
):
    from uuid import uuid4

    assert parse_dsn(os.environ["AUTONOMY_TEST_DSN"])["dbname"].endswith("_test")
    op = prepared(store, identity)
    socket = tmp_path / "runtime" / "broker.sock"
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "AUTONOMY_TEST_DSN",
            "PLAYWRIGHT_BROWSERS_PATH",
            "ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE",
        )
        if key in os.environ
    }
    env.update(
        ROBOTHOR_AUTONOMY_SOCKET=str(socket), GENUS_AUTH_SIGNING_KEY="fixture-only-signing-key-" * 3
    )

    def launch():
        proc = subprocess.Popen(
            [sys.executable, "-c", SERVICE_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://autonomy", timeout=0.5
        ) as client:
            for _ in range(100):
                if proc.poll() is not None:
                    raise AssertionError(proc.communicate())
                try:
                    if client.get("/ready").status_code == 200:
                        return proc
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        proc.terminate()
        proc.communicate(timeout=10)
        pytest.fail("broker did not become ready")

    def call(request):
        # A new controller process on every invocation; no Python objects survive.
        output = subprocess.run(
            [sys.executable, "-c", CONTROLLER_SCRIPT],
            input=json.dumps({"scope": identity.model_dump(), "request": request}),
            text=True,
            capture_output=True,
            env=env,
            timeout=40,
        )
        assert output.returncode == 0, output.stderr
        return json.loads(output.stdout)

    proc = launch()
    try:
        opened = call(
            {"kind": "open", "operation_id": op["id"], "url": "https://form.example/apply"}
        )
        wid = opened["workflow_id"]
        advanced = call(
            {
                "kind": "execute",
                "workflow_id": wid,
                "command_id": str(uuid4()),
                "revision": 0,
                "advance": True,
                "plan": {"url": "https://form.example/apply", "submit_selector": "#next"},
            }
        )
        assert advanced["reason"] == "workflow_step_completed", advanced
        proc.send_signal(signal.SIGUSR1)
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://autonomy"
        ) as client:
            for _ in range(50):
                ready = client.get("/ready").json()
                if not ready["accepting"]:
                    break
                time.sleep(0.02)
            assert not ready["accepting"]
            assert ready["active_workflows"] == 1
        assert call(
            {"kind": "open", "operation_id": op["id"], "url": "https://form.example/apply"}
        ) == {"error": "workflow_broker_draining"}
        inspected = call({"kind": "inspect", "workflow_id": wid})
        assert any(field["id"] == "two" for field in inspected["fields"])
        assert not any(field["id"] == "one" for field in inspected["fields"])
        command = {
            "kind": "execute",
            "workflow_id": wid,
            "command_id": str(uuid4()),
            "revision": 1,
            "plan": {"url": "https://form.example/apply", "submit_selector": "#submit"},
        }
        completed = call(command)
        assert completed["state"] == "completed", completed
        assert call(command) == completed
        assert proc.poll() is None
        proc.send_signal(signal.SIGUSR2)
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://autonomy"
        ) as client:
            for _ in range(50):
                ready = client.get("/ready").json()
                if ready["accepting"]:
                    break
                time.sleep(0.02)
            assert ready["accepting"] and ready["active_workflows"] == 0

        row = store.operation(identity, op["id"])
        another = store.reserve(
            identity,
            row["grant_id"],
            "main",
            WebOperation.model_validate({**row["proposal"], "idempotency_key": "restart-fixture"}),
        )
        opened = call(
            {"kind": "open", "operation_id": another["id"], "url": "https://form.example/apply"}
        )
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=12)
        assert b"step-two" not in stdout + stderr
        proc = launch()
        result = call({"kind": "status", "workflow_id": opened["workflow_id"]})
        assert result["state"] == "lost"
        assert result["operation_state"] == "reconciling"
        assert call(command) == completed
    finally:
        proc.terminate()
        proc.communicate(timeout=12)
