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

## Known blocked domains
- cnbc.com
- Likely: wsj.com, bloomberg.com, reuters.com, nytimes.com (news sites with anti-bot measures)

## Notes
- The browser tool uses a real Chromium session with proper headers, so it bypasses bot detection
- Do NOT retry `web_fetch` — it will fail again on the same domain
- If the browser also fails, the content may be behind a paywall requiring login
