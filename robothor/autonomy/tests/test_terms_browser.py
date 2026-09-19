"""Real Chromium snapshots are private and precede the external submit click."""

import json

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.tests.test_terms_audit import operation


@pytest.mark.timeout(60)
async def test_submission_records_rendered_terms_and_unfetched_links(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit

    op = operation(store, identity)
    html = """<body><p>Membership requires annual renewal.</p>
    <a href="/terms?member=private-member">Full membership terms</a>
    <label><input id="terms" type="checkbox">Accept membership conditions</label>
    <button id="submit" onclick="document.querySelector('#done').hidden=false">Apply</button>
    <p id="done" hidden>Application received</p></body>"""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://club.example/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        result = await BrowserBroker(store).execute_on_page(
            identity,
            op["id"],
            "main",
            ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                check_selectors=["#terms"],
                success_selector="#done",
                success_text="Application received",
            ),
            page,
        )
        await browser.close()
    assert result["state"] == "completed"
    audit = TermsAudit(store)
    rows = audit.list(identity, op["id"])
    assert [r["phase"] for r in rows] == ["before_input", "before_submit"]
    assert "private-member" not in json.dumps(result)
    snapshot = audit.read(identity, op["id"], rows[-1]["id"])["snapshot"]
    assert snapshot["coverage"] == "visible_text_only"
    assert "annual renewal" in snapshot["documents"][0]["text"]
    assert snapshot["documents"][0]["links"] == ["https://club.example/terms?member=private-member"]


@pytest.mark.timeout(60)
async def test_after_transient_code_no_page_text_is_persisted(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit
    from robothor.autonomy.terms_capture import capture_terms

    op = operation(store, identity)
    broker = BrowserBroker(store)
    broker._used_transient_code = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<body>Card verification code 123</body>")
        assert (
            await capture_terms(
                broker,
                identity,
                op["id"],
                "main",
                page,
                "https://club.example",
                frozenset(),
                phase="before_submit",
            )
            is None
        )
        await browser.close()
    assert TermsAudit(store).list(identity, op["id"]) == []


@pytest.mark.timeout(60)
async def test_large_pages_record_explicit_truncation_without_blocking(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit
    from robothor.autonomy.terms_capture import capture_terms

    op = operation(store, identity)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://club.example/**",
            lambda route: route.fulfill(
                body="<body>" + ("Terms " * 40000) + "</body>", content_type="text/html"
            ),
        )
        await page.goto("https://club.example/apply")
        ref = await capture_terms(
            BrowserBroker(store),
            identity,
            op["id"],
            "main",
            page,
            "https://club.example",
            frozenset(),
            phase="before_input",
        )
        await browser.close()
    saved = TermsAudit(store).read(identity, op["id"], ref["id"])["snapshot"]
    assert saved["documents"][0]["text_truncated"]
    assert len(saved["documents"][0]["text"].encode()) <= 100000


@pytest.mark.timeout(60)
async def test_real_code_entry_keeps_only_the_pre_input_snapshot(store, identity):
    from robothor.autonomy.broker import Challenge
    from robothor.autonomy.terms_audit import TermsAudit

    op = operation(store, identity)
    html = """<body><p>Confirm the membership terms.</p><input id="code" oninput="document.querySelector('#echo').textContent=btoa(this.value)">
    <p id="echo"></p><button id="submit" onclick="document.querySelector('#done').hidden=false">Apply</button>
    <p id="done" hidden>Application received</p></body>"""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://club.example/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        result = await BrowserBroker(store).execute_on_page(
            identity,
            op["id"],
            "main",
            ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                success_selector="#done",
                success_text="Application received",
                challenge=Challenge(selector="#code", kind="one_time_code"),
            ),
            page,
            verification_code="123456",
        )
        await browser.close()
    assert result["state"] == "completed"
    audit = TermsAudit(store)
    rows = audit.list(identity, op["id"])
    assert [row["phase"] for row in rows] == ["before_input"]
    saved = json.dumps(audit.read(identity, op["id"], rows[0]["id"]))
    assert "123456" not in saved and "MTIzNDU2" not in saved
