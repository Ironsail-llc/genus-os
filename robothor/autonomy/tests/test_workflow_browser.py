"""A client-side wizard stays alive across commands and validates before submission."""

import base64
import json
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import ExecutionPlan, FieldBinding
from robothor.autonomy.models import ResourceInput
from robothor.autonomy.tests.test_workflow_store import prepared
from robothor.autonomy.workflows.manager import WorkflowManager


@pytest.mark.timeout(60)
async def test_multistep_form_validation_upload_and_duplicate_command(store, identity, monkeypatch):
    from robothor.auth import tokens
    from robothor.autonomy.workflows import client
    from robothor.autonomy.workflows.api import create_app
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy

    monkeypatch.setattr(tokens, "signing_key", lambda: "fixture-only-signing-key-" * 3)
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(
        autonomy,
        "scope_for_actor",
        lambda tenant, actor: identity.model_copy(update={"tenant_id": tenant, "owner_id": actor}),
    )
    op = prepared(store, identity)
    profile = store.put_resource(
        identity,
        ResourceInput(
            kind="profile",
            label="Personal profile",
            payload=json.dumps({"first_name": "Alice", "email": "alice@example.com"}),
        ),
    )
    invalid = store.put_resource(
        identity,
        ResourceInput(
            kind="profile", label="Invalid fixture", payload=json.dumps({"email": "not-an-email"})
        ),
    )
    photo = store.put_resource(
        identity,
        ResourceInput(
            kind="document",
            label="Photo",
            payload=json.dumps(
                {
                    "name": "portrait.png",
                    "mime_type": "image/png",
                    "base64": base64.b64encode(b"fixture photo").decode(),
                }
            ),
        ),
    )
    html = """<form id="first"><input id="name" required><button id="next">Next</button></form>
    <form id="second" hidden><input id="email" type="email" required><input id="photo" type="file" required><button id="submit">Apply</button></form><p id="confirmation"></p>
    <script>document.querySelector('#first').onsubmit=e=>{e.preventDefault();document.querySelector('#first').hidden=true;document.querySelector('#second').hidden=false;};
    document.querySelector('#second').onsubmit=async e=>{e.preventDefault();await fetch('/submit',{method:'POST',body:JSON.stringify({name:document.querySelector('#name').value,email:document.querySelector('#email').value,photo:document.querySelector('#photo').files[0].name})});document.querySelector('#confirmation').textContent='Application received';};</script>"""
    submissions = []

    async def respond(route):
        if route.request.url == "https://form.example/submit":
            submissions.append(json.loads(route.request.post_data))
            await route.fulfill(body="ok")
        else:
            await route.fulfill(content_type="text/html", body=html)

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=respond)
        app = create_app(manager)
        real_invoke = client.invoke

        async def rpc(scope, agent_id, request):
            # Each call gets a fresh controller connection; the service owns the page.
            return await real_invoke(
                scope, agent_id, request, transport=httpx.ASGITransport(app=app)
            )

        monkeypatch.setattr(client, "invoke", rpc)

        async def call(kind, **request):
            return await autonomy.handle(
                {"kind": "workflow_" + kind, **request},
                ToolContext(
                    tenant_id=identity.tenant_id, user_id=identity.owner_id, agent_id="main"
                ),
            )

        async def execute(command_id, revision, plan, *, advance=False):
            return await call(
                "execute",
                workflow_id=wid,
                command_id=command_id,
                revision=revision,
                plan=plan.model_dump(mode="json"),
                advance=advance,
            )

        opened = await call("open", operation_id=op["id"], url="https://form.example/apply")
        wid = opened["workflow_id"]
        next_plan = ExecutionPlan(
            url="https://form.example/apply",
            submit_selector="#next",
            fields=[
                FieldBinding(
                    selector="#name", resource_id=profile["id"], kind="profile", field="first_name"
                )
            ],
        )
        command = str(uuid4())
        step = await execute(command, 0, next_plan, advance=True)
        assert step["reason"] == "workflow_step_completed", step
        assert step["revision"] == 1
        assert await execute(command, 0, next_plan, advance=True) == step
        inspected = await call("inspect", workflow_id=wid)
        assert any(field["id"] == "email" for field in inspected["fields"])
        assert not any(field["id"] == "name" for field in inspected["fields"])
        plan = ExecutionPlan(
            url="https://form.example/apply",
            submit_selector="#submit",
            fields=[
                FieldBinding(
                    selector="#email", resource_id=invalid["id"], kind="profile", field="email"
                ),
                FieldBinding(
                    selector="#photo",
                    resource_id=photo["id"],
                    kind="document",
                    field="file",
                    method="upload",
                ),
            ],
        )
        result = await execute(str(uuid4()), 1, plan)
        assert result["reason"] == "validation_required", result
        assert submissions == []
        corrected = ExecutionPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "fields": [
                    {**plan.fields[0].model_dump(mode="json"), "resource_id": profile["id"]},
                    plan.fields[1].model_dump(mode="json"),
                ],
            }
        )
        final_command = str(uuid4())
        result = await execute(final_command, 1, corrected)
        assert result["state"] == "completed", result
        assert submissions == [
            {"name": "Alice", "email": "alice@example.com", "photo": "portrait.png"}
        ]
        assert await execute(final_command, 1, corrected) == result
        assert len(submissions) == 1
        assert manager.active_count == 0
        await manager.shutdown()


async def test_secure_code_resume_keeps_page_and_does_not_persist_code(
    store, identity, monkeypatch
):
    from robothor.auth import tokens
    from robothor.autonomy import runtime
    from robothor.autonomy.workflows import client
    from robothor.autonomy.workflows.api import create_app

    monkeypatch.setattr(tokens, "signing_key", lambda: "fixture-only-signing-key-" * 3)
    monkeypatch.setattr(runtime, "AutonomyStore", lambda: store)
    op = prepared(store, identity)
    posted = []

    async def respond(route):
        if route.request.url == "https://form.example/submit":
            posted.append(route.request.post_data)
            await route.fulfill(body="ok")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""<input id="code"><button id="submit" onclick="fetch('/submit',{method:'POST',body:document.querySelector('#code').value}).then(()=>{document.querySelector('p').textContent='Application received';})">Apply</button><p></p>""",
            )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=respond)
        app = create_app(manager)
        real_invoke = client.invoke

        async def rpc(scope, agent_id, request):
            return await real_invoke(
                scope, agent_id, request, transport=httpx.ASGITransport(app=app)
            )

        monkeypatch.setattr(client, "invoke", rpc)
        opened = await manager.open(identity, "main", op["id"], "https://form.example/apply")
        plan = ExecutionPlan.model_validate(
            {
                "url": "https://form.example/apply",
                "submit_selector": "#submit",
                "challenge": {"selector": "#code", "kind": "one_time_code"},
            }
        )
        waiting = await manager.execute(
            identity, "main", opened["workflow_id"], str(uuid4()), 0, plan
        )
        assert waiting["state"] == "awaiting_input"
        store.resume_with_code(identity, op["id"])
        result = await runtime.run_browser(
            identity, op["id"], "main", plan, verification_code="836214"
        )
        assert result["state"] == "completed", result
        assert posted == ["836214"]
        with store.transaction() as cur:
            cur.execute(
                "SELECT request,result FROM autonomy_workflow_commands WHERE workflow_id=%s",
                (opened["workflow_id"],),
            )
            assert "836214" not in json.dumps([dict(row) for row in cur.fetchall()])
        assert manager.active_count == 0
        await manager.shutdown()
