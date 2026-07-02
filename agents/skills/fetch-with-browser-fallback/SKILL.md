---
name: fetch-with-browser-fallback
description: Fetch a URL that blocks web_fetch by falling back to the browser tool
tags:
- web
- fallback
- browser
tools_required:
- browser
---

## When to use
When `web_fetch` returns 403 Forbidden (bot detection) on a URL.

## Steps
1. Call `browser(action="start")` to launch the browser session
2. Call `browser(action="navigate", url="<target_url>")` to load the page
3. Call `browser(action="snapshot")` to extract the page content as an accessibility tree
4. Call `browser(action="stop")` to close the browser session
5. Parse the article content from the snapshot (look for heading + paragraph text)

## When `web_fetch` returns anti-bot or empty cache
If `web_fetch` returns a 200 status but the content is an anti-bot redirect or empty cache:
1. Check the content for anti-bot phrases (e.g., "Please click here if you are not redirected within a few seconds")
2. Try the Wayback Machine API: `https://archive.org/wayback/available?url=<target_url>`
3. If no archive, search for the site's documentation or API docs
4. Use the browser to navigate to the documentation and find the specific page

## Known blocked domains (browser also fails)
- cnbc.com
- goodrx.com — aggressive Cloudflare/WAF, blocks both `web_fetch` and headless Chromium. Use `web_search` index instead.
- Likely: wsj.com, bloomberg.com, reuters.com, nytimes.com (news sites with anti-bot measures)

## Notes
- The browser tool uses a real Chromium session with proper headers, so it bypasses most bot detection
- Do NOT retry `web_fetch` — it will fail again on the same domain
- If the browser also fails, the content may be behind a paywall requiring login, or the site has aggressive anti-bot (WAF). Fall back to `web_search` for indexed content.
- Google Cache may return anti-bot redirects even with a 200 status — check content before assuming success
