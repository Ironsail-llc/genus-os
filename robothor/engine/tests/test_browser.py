"""Tests for browser automation tool — ARIA distillation, ref resolution, vision fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.handlers.browser import (
    ElementRef,
    _build_shadow_distilled,
    _distill_snapshot,
    _resolve_ref,
)

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_aria_yaml():
    """Realistic ARIA snapshot YAML from a form page."""
    return """\
- navigation "Main":
  - list:
    - listitem:
      - link "Home"
    - listitem:
      - link "About"
- main:
  - heading "Apply for Position" [level=1]
  - form "Application":
    - textbox "First Name" [required]
    - textbox "Last Name" [required]
    - combobox "Country"
    - checkbox "I agree to terms"
    - button "Submit"
  - paragraph: Please fill out all required fields."""


@pytest.fixture
def workday_aria_yaml():
    """Deeply nested ARIA tree mimicking Workday's structure."""
    return """\
- navigation "Global Navigation":
  - link "Home"
  - link "Careers"
  - button "Search"
- main:
  - heading "Head of AI Orchestration" [level=1]
  - region "Application Form":
    - group:
      - group:
        - textbox "Legal First Name" [required]
        - textbox "Legal Last Name" [required]
    - group:
      - textbox "Email Address" [required]
      - textbox "Phone Number"
    - combobox "Country" [required]
    - group:
      - radio "Yes"
      - radio "No"
    - button "Next"
    - button "Save Draft"
  - paragraph: Fields marked with * are required."""


@pytest.fixture
def empty_aria():
    return ""


@pytest.fixture
def minimal_aria():
    return '- heading "Page Title" [level=1]'


# ─── Tests: ARIA Snapshot Parsing ────────────────────────────────────


class TestDistillSnapshot:
    def test_parses_simple_form(self, sample_aria_yaml):
        distilled, registry = _distill_snapshot(sample_aria_yaml)
        # Should find: 2 links + 2 textboxes + 1 combobox + 1 checkbox + 1 button = 7
        assert len(registry) == 7
        assert registry[1].role == "link"
        assert registry[1].name == "Home"
        assert registry[3].role == "textbox"
        assert registry[3].name == "First Name"
        assert registry[7].role == "button"
        assert registry[7].name == "Submit"

    def test_assigns_sequential_indices(self, sample_aria_yaml):
        _, registry = _distill_snapshot(sample_aria_yaml)
        indices = sorted(registry.keys())
        assert indices == list(range(1, len(registry) + 1))

    def test_filters_non_interactive(self, sample_aria_yaml):
        _, registry = _distill_snapshot(sample_aria_yaml)
        roles = {ref.role for ref in registry.values()}
        # heading, paragraph, form, navigation should NOT be in registry
        assert "heading" not in roles
        assert "paragraph" not in roles
        assert "navigation" not in roles

    def test_preserves_structural_context(self, sample_aria_yaml):
        distilled, _ = _distill_snapshot(sample_aria_yaml)
        # Headings and landmarks should appear in output text
        assert "heading" in distilled
        assert "Apply for Position" in distilled
        assert "navigation" in distilled

    def test_interactive_elements_have_refs(self, sample_aria_yaml):
        distilled, _ = _distill_snapshot(sample_aria_yaml)
        assert "@1" in distilled
        assert "@7" in distilled

    def test_handles_empty_tree(self, empty_aria):
        distilled, registry = _distill_snapshot(empty_aria)
        assert registry == {}
        assert "empty" in distilled.lower()

    def test_handles_minimal_tree(self, minimal_aria):
        distilled, registry = _distill_snapshot(minimal_aria)
        assert registry == {}  # heading is not interactive
        assert "heading" in distilled

    def test_workday_nested_form(self, workday_aria_yaml):
        distilled, registry = _distill_snapshot(workday_aria_yaml)
        # Should find: 3 links + 1 button(Search) + 4 textboxes + 1 combobox + 2 radios + 2 buttons = 13
        assert len(registry) >= 10  # at least the core elements
        # Verify key form fields are captured
        roles = {ref.role for ref in registry.values()}
        assert "textbox" in roles
        assert "combobox" in roles
        assert "button" in roles
        assert "radio" in roles

    def test_attributes_preserved(self, sample_aria_yaml):
        _, registry = _distill_snapshot(sample_aria_yaml)
        # textbox "First Name" [required] should have required attribute
        first_name = registry[3]
        assert first_name.attributes.get("required") == "true"

    def test_caps_at_max_elements(self):
        """Generate 110 interactive elements and verify cap at 100."""
        lines = [f'- button "Button {i}"' for i in range(110)]
        raw = "\n".join(lines)
        distilled, registry = _distill_snapshot(raw)
        assert len(registry) == 100
        assert "more interactive elements" in distilled

    def test_paragraph_text_preserved(self, sample_aria_yaml):
        distilled, _ = _distill_snapshot(sample_aria_yaml)
        assert "required fields" in distilled.lower()


# ─── Tests: Ref Resolution ──────────────────────────────────────────


class TestRefResolution:
    @pytest.fixture
    def mock_page(self):
        page = AsyncMock()
        return page

    @pytest.fixture
    def sample_registry(self):
        return {
            1: ElementRef(1, "textbox", "First Name", ""),
            2: ElementRef(2, "button", "Submit", ""),
            3: ElementRef(3, "link", "Home", ""),
            4: ElementRef(4, "combobox", "Country", ""),
            5: ElementRef(5, "textbox", "", ""),  # unnamed textbox
        }

    @pytest.mark.asyncio
    async def test_resolves_by_role_and_name(self, mock_page, sample_registry):
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_locator
        mock_page.get_by_role = MagicMock(return_value=mock_locator)

        result = await _resolve_ref(mock_page, sample_registry, "@1")
        assert result is not None
        mock_page.get_by_role.assert_called_with("textbox", name="First Name")

    @pytest.mark.asyncio
    async def test_resolves_button_by_text_fallback(self, mock_page, sample_registry):
        # Strategy 1 fails (no role+name match)
        no_match = AsyncMock()
        no_match.count = AsyncMock(return_value=0)
        # Strategy 2 fails (multiple buttons)
        multi_match = AsyncMock()
        multi_match.count = AsyncMock(return_value=3)
        mock_page.get_by_role = MagicMock(side_effect=[no_match, multi_match])
        # Strategy 4: get_by_text succeeds
        text_locator = AsyncMock()
        text_locator.count = AsyncMock(return_value=1)
        text_locator.first = text_locator
        mock_page.get_by_text = MagicMock(return_value=text_locator)

        result = await _resolve_ref(mock_page, sample_registry, "@2")
        assert result is not None
        mock_page.get_by_text.assert_called_with("Submit", exact=True)

    @pytest.mark.asyncio
    async def test_resolves_textbox_by_label(self, mock_page, sample_registry):
        # Strategy 1 fails
        no_match = AsyncMock()
        no_match.count = AsyncMock(return_value=0)
        # Strategy 2 fails
        multi_match = AsyncMock()
        multi_match.count = AsyncMock(return_value=5)
        mock_page.get_by_role = MagicMock(side_effect=[no_match, multi_match])
        # Strategy 3: get_by_label succeeds
        label_locator = AsyncMock()
        label_locator.count = AsyncMock(return_value=1)
        label_locator.first = label_locator
        mock_page.get_by_label = MagicMock(return_value=label_locator)

        result = await _resolve_ref(mock_page, sample_registry, "@1")
        assert result is not None
        mock_page.get_by_label.assert_called_with("First Name")

    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_ref(self, mock_page, sample_registry):
        result = await _resolve_ref(mock_page, sample_registry, "@999")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_bad_format(self, mock_page, sample_registry):
        result = await _resolve_ref(mock_page, sample_registry, "foo")
        assert result is None

    @pytest.mark.asyncio
    async def test_unique_role_match(self, mock_page, sample_registry):
        """When role+name fails but role alone is unique, use it."""
        no_name_match = AsyncMock()
        no_name_match.count = AsyncMock(return_value=0)
        unique_match = AsyncMock()
        unique_match.count = AsyncMock(return_value=1)
        unique_match.first = unique_match
        mock_page.get_by_role = MagicMock(side_effect=[no_name_match, unique_match])

        result = await _resolve_ref(mock_page, sample_registry, "@4")
        assert result is not None


# ─── Tests: Vision Fallback ──────────────────────────────────────────


class TestVisionFallback:
    @pytest.mark.asyncio
    async def test_includes_screenshot_when_few_elements(self):
        """When ARIA tree has <= 2 interactive elements, screenshot is included."""
        from robothor.engine.tools.handlers.browser import _action_snapshot

        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Example")

        # ARIA returns just a heading (0 interactive elements)
        mock_locator = AsyncMock()
        mock_locator.aria_snapshot = AsyncMock(return_value='- heading "Test" [level=1]')
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.screenshot = AsyncMock(return_value=b"fake_png_data")
        mock_page.evaluate = AsyncMock(return_value=10)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {}

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_snapshot({}, ctx)

        assert result.get("vision_fallback") is True
        assert "screenshot_base64" in result
        assert result["element_count"] == 0

    @pytest.mark.asyncio
    async def test_no_screenshot_when_many_elements(self):
        """When ARIA tree has many interactive elements, no screenshot."""
        from robothor.engine.tools.handlers.browser import _action_snapshot

        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Form")

        # ARIA returns 5 interactive elements
        aria_content = "\n".join(
            [
                '- textbox "Field 1"',
                '- textbox "Field 2"',
                '- textbox "Field 3"',
                '- button "Submit"',
                '- link "Cancel"',
            ]
        )
        mock_locator = AsyncMock()
        mock_locator.aria_snapshot = AsyncMock(return_value=aria_content)
        mock_page.locator = MagicMock(return_value=mock_locator)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {}

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_snapshot({}, ctx)

        assert "screenshot_base64" not in result
        assert result["element_count"] == 5

    @pytest.mark.asyncio
    async def test_full_fallback_on_aria_exception(self):
        """When aria_snapshot raises, return screenshot-only."""
        from robothor.engine.tools.handlers.browser import _action_snapshot

        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Broken")

        mock_locator = AsyncMock()
        mock_locator.aria_snapshot = AsyncMock(side_effect=RuntimeError("boom"))
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.screenshot = AsyncMock(return_value=b"fallback_png")

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {}

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_snapshot({}, ctx)

        assert result.get("vision_fallback") is True
        assert "screenshot_base64" in result
        assert result["element_count"] == 0
        assert "error_detail" in result


# ─── Tests: Navigate Wait Strategy ──────────────────────────────────


class TestNavigateWaitStrategy:
    @pytest.mark.asyncio
    async def test_uses_networkidle_first(self):
        from robothor.engine.tools.handlers.browser import _action_navigate

        mock_response = MagicMock()
        mock_response.status = 200
        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Example")
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.wait_for_selector = AsyncMock()

        session = MagicMock()
        session.page = mock_page

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_navigate({"url": "https://example.com"}, ctx)

        assert result["status"] == 200
        # First call should be networkidle
        mock_page.goto.assert_called_once_with(
            "https://example.com", wait_until="networkidle", timeout=20000
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_domcontentloaded(self):
        from robothor.engine.tools.handlers.browser import _action_navigate

        mock_response = MagicMock()
        mock_response.status = 200
        mock_page = AsyncMock()
        mock_page.url = "https://streaming.example.com"
        mock_page.title = AsyncMock(return_value="Streaming")
        # First call (networkidle) times out, second (domcontentloaded) succeeds
        mock_page.goto = AsyncMock(side_effect=[TimeoutError("networkidle timeout"), mock_response])
        mock_page.wait_for_selector = AsyncMock()

        session = MagicMock()
        session.page = mock_page

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_navigate({"url": "https://streaming.example.com"}, ctx)

        assert result["status"] == 200
        assert mock_page.goto.call_count == 2

    @pytest.mark.asyncio
    async def test_waits_for_interactive_elements(self):
        from robothor.engine.tools.handlers.browser import _action_navigate

        mock_response = MagicMock()
        mock_response.status = 200
        mock_page = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Example")
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.wait_for_selector = AsyncMock()

        session = MagicMock()
        session.page = mock_page

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            await _action_navigate({"url": "https://example.com"}, ctx)

        mock_page.wait_for_selector.assert_called_once()


# ─── Tests: Act with Refs ────────────────────────────────────────────


class TestActWithRefs:
    @pytest.mark.asyncio
    async def test_click_by_ref(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_locator
        mock_locator.click = AsyncMock()

        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=mock_locator)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {
            1: ElementRef(1, "button", "Submit", ""),
        }

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act({"request": {"kind": "click", "ref": "@1"}}, ctx)

        assert result["acted"] == "click"
        assert "@1" in result["target"]

    @pytest.mark.asyncio
    async def test_fill_by_ref(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_locator
        mock_locator.fill = AsyncMock()

        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=mock_locator)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {
            3: ElementRef(3, "textbox", "First Name", ""),
        }

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act(
                {"request": {"kind": "fill", "ref": "@3", "value": "Alice"}}, ctx
            )

        assert result["acted"] == "fill"
        mock_locator.fill.assert_called_once_with("Alice", timeout=10000)

    @pytest.mark.asyncio
    async def test_batch_fill_by_refs(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.first = mock_locator
        mock_locator.fill = AsyncMock()

        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=mock_locator)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {
            1: ElementRef(1, "textbox", "First Name", ""),
            2: ElementRef(2, "textbox", "Last Name", ""),
        }

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act(
                {
                    "request": {
                        "kind": "fill",
                        "fields": [
                            {"ref": "@1", "value": "Alice"},
                            {"ref": "@2", "value": "TestUser"},
                        ],
                    }
                },
                ctx,
            )

        assert result["acted"] == "fill"
        assert result["fields_filled"] == 2
        assert result["fields_requested"] == 2

    @pytest.mark.asyncio
    async def test_stale_ref_returns_error(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_page = AsyncMock()
        # All resolution strategies fail
        no_match = AsyncMock()
        no_match.count = AsyncMock(return_value=0)
        mock_page.get_by_role = MagicMock(return_value=no_match)
        mock_page.get_by_label = MagicMock(return_value=no_match)
        mock_page.get_by_placeholder = MagicMock(return_value=no_match)
        mock_page.get_by_text = MagicMock(return_value=no_match)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {
            1: ElementRef(1, "textbox", "Gone Field", ""),
        }

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act({"request": {"kind": "click", "ref": "@1"}}, ctx)

        assert "error" in result
        assert "Could not resolve" in result["error"]

    @pytest.mark.asyncio
    async def test_click_by_coordinates_still_works(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_page = AsyncMock()
        mock_page.mouse = AsyncMock()
        mock_page.mouse.click = AsyncMock()

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {}

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act({"request": {"kind": "click", "x": 640, "y": 480}}, ctx)

        assert result["acted"] == "click"
        mock_page.mouse.click.assert_called_once_with(640, 480)

    @pytest.mark.asyncio
    async def test_css_selector_still_works(self):
        from robothor.engine.tools.handlers.browser import _action_act

        mock_locator = AsyncMock()
        mock_locator.first = mock_locator
        mock_locator.click = AsyncMock()

        mock_page = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        session = MagicMock()
        session.page = mock_page
        session.element_registry = {}

        ctx = MagicMock()
        ctx.agent_id = "test"

        with patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await _action_act(
                {"request": {"kind": "click", "selector": "#submit-btn"}}, ctx
            )

        assert result["acted"] == "click"
        mock_page.locator.assert_called_with("#submit-btn")


# ─── Tests: Shadow DOM Fallback ──────────────────────────────────────


class TestShadowDomFallback:
    def test_build_shadow_distilled(self):
        js_elements = [
            {"role": "textbox", "name": "Email", "type": "email"},
            {"role": "button", "name": "Submit", "type": ""},
            {"role": "input", "name": "", "type": "hidden"},
        ]
        distilled, registry = _build_shadow_distilled(js_elements)
        assert len(registry) == 3
        assert "@1" in distilled
        assert "Email" in distilled
        assert registry[1].role == "textbox"
        assert registry[2].name == "Submit"

    def test_empty_js_elements(self):
        distilled, registry = _build_shadow_distilled([])
        assert registry == {}
        assert "shadow dom" in distilled.lower()


# ─── Isolated background page (web_search's results-page fallback) ────
#
# A session holds ONE page and the @N registry is bound to whatever is on it.
# Any background use of the browser must therefore get its own tab: navigating
# session.page would move the agent's document out from under refs it is still
# using, and leave it parked on a search engine.


class _StubPage:
    def __init__(
        self,
        *,
        evaluate_result=None,
        evaluate_error: Exception | None = None,
        wait_error: Exception | None = None,
    ):
        self.url = "https://results.example.com/search"
        self.closed = False
        self.goto_calls: list[str] = []
        self.evaluated: list[str] = []
        self.waited_for: list[str] = []
        self._evaluate_result = evaluate_result if evaluate_result is not None else []
        self._evaluate_error = evaluate_error
        self._wait_error = wait_error

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        return MagicMock(status=200)

    async def wait_for_selector(self, selector, timeout=None):
        self.waited_for.append(selector)
        if self._wait_error is not None:
            raise self._wait_error
        return MagicMock()

    async def evaluate(self, js):
        self.evaluated.append(js)
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return self._evaluate_result

    async def close(self):
        self.closed = True


class _StubContext:
    def __init__(self, page):
        self._page = page
        self.new_pages = 0

    async def new_page(self):
        self.new_pages += 1
        return self._page


def _install_stub_session(agent_id: str, new_page):
    """Put a session holding stub objects in the module's session table."""
    from robothor.engine.tools.handlers import browser as browser_mod

    agent_page = _StubPage()
    agent_page.url = "https://intranet.example.com/form"
    session = browser_mod.BrowserSession(
        browser=MagicMock(),
        context=_StubContext(new_page),
        page=agent_page,
    )
    session.element_registry = {1: browser_mod.ElementRef(1, "textbox", "Name", "")}
    browser_mod._sessions[agent_id] = session
    return session


@pytest.fixture
def stub_session_cleanup():
    from robothor.engine.tools.handlers import browser as browser_mod

    yield
    browser_mod._sessions.pop("isolation-test", None)


async def test_isolated_fetch_uses_a_new_tab_and_closes_it(stub_session_cleanup):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser as browser_mod

    tab = _StubPage(evaluate_result=[{"title": "t", "url": "https://example.com/", "snippet": "s"}])
    session = _install_stub_session("isolation-test", tab)

    out = await browser_mod.isolated_fetch(
        ToolContext(agent_id="isolation-test"), "https://www.bing.com/search?q=x", "() => 1"
    )

    assert out["result"] == [{"title": "t", "url": "https://example.com/", "snippet": "s"}]
    assert session.context.new_pages == 1
    assert tab.closed is True
    # The agent's own page never moved, and its refs are intact.
    assert session.page.goto_calls == []
    assert session.page.url == "https://intranet.example.com/form"
    assert 1 in session.element_registry


async def test_isolated_fetch_closes_its_tab_when_evaluation_fails(stub_session_cleanup):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser as browser_mod

    tab = _StubPage(evaluate_error=RuntimeError("CSP blocked eval"))
    _install_stub_session("isolation-test", tab)

    out = await browser_mod.isolated_fetch(
        ToolContext(agent_id="isolation-test"), "https://www.bing.com/search?q=x", "() => 1"
    )

    assert "error" in out
    assert tab.closed is True


async def test_isolated_fetch_waits_for_the_rows_before_reading_them(stub_session_cleanup):
    """Startpage answers with a proof-of-work interstitial that clears itself.

    Evaluating at domcontentloaded reads the interstitial and calls the source
    empty; waiting for the result anchors is what tells "still loading" apart
    from "nothing here".
    """
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser as browser_mod

    tab = _StubPage(evaluate_result=[{"title": "t", "url": "https://example.com/", "snippet": ""}])
    _install_stub_session("isolation-test", tab)

    out = await browser_mod.isolated_fetch(
        ToolContext(agent_id="isolation-test"),
        "https://www.startpage.com/do/search?q=x",
        "() => 1",
        wait_selector="a.result-link",
    )

    assert tab.waited_for == ["a.result-link"]
    assert out["result"]


async def test_isolated_fetch_reads_a_page_whose_rows_never_appear(stub_session_cleanup):
    """A page that really has no results is still read, not turned into an error."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser as browser_mod

    tab = _StubPage(evaluate_result=[], wait_error=RuntimeError("Timeout 6000ms exceeded"))
    _install_stub_session("isolation-test", tab)

    out = await browser_mod.isolated_fetch(
        ToolContext(agent_id="isolation-test"),
        "https://www.startpage.com/do/search?q=x",
        "() => 1",
        html_js="() => 'html'",
        wait_selector="a.result-link",
    )

    assert tab.waited_for == ["a.result-link"]
    assert out["result"] == []
    assert tab.evaluated == ["() => 1", "() => 'html'"]  # the page was still read
    assert "error" not in out


async def test_isolated_fetch_without_a_session_does_not_start_one():
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser as browser_mod

    out = await browser_mod.isolated_fetch(
        ToolContext(agent_id="no-session-here"), "https://example.com/", "() => 1"
    )

    assert out["error"] == "Browser not started."
    assert "no-session-here" not in browser_mod._sessions
