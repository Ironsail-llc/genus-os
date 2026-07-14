/**
 * The test environment must be able to sanitize. Otherwise the suite is hollow.
 *
 * `srcdoc-renderer.tsx` runs DOMPurify over LLM-generated HTML before putting it
 * in an iframe srcdoc. That is an XSS boundary, and it had **no test coverage at
 * all**.
 *
 * Worse, the environment could not have tested it. DOMPurify >= 3.4.x reads
 * `nodeName` through an unbound `Node.prototype` getter (DOM-clobbering
 * hardening). **happy-dom returns `""` from that base getter**, so every tag
 * resolves to the empty string, the first element is removed, happy-dom's
 * NodeIterator then dies mid-walk, and *everything after it passes through
 * unsanitized*.
 *
 * The failure is silent: `isSupported` stays `true` while sanitization quietly
 * degrades to a no-op. A suite running under happy-dom would happily go green
 * with the XSS filter switched off — which is exactly what made the 3.4.11 bump
 * (#162) look dangerous when the bug was in the test environment, not the library.
 *
 * Production uses `isomorphic-dompurify`, which is jsdom. So the tests now use
 * jsdom too, and these assertions are the canary: if sanitization is ever no-op'd
 * again — by an environment change, a bad upgrade, a mis-wired config — this
 * fails loudly instead of going green.
 */

import DOMPurify from "dompurify";
import { describe, expect, it } from "vitest";

describe("the test environment can actually sanitize", () => {
  it("reports itself as supported", () => {
    expect(DOMPurify.isSupported).toBe(true);
  });

  it("strips a script tag", () => {
    const out = DOMPurify.sanitize('<p>hi</p><script>alert(1)</script>');
    expect(out).not.toContain("<script");
    expect(out).toContain("hi");
  });

  it("strips an iframe — the canary for a no-op'd sanitizer", () => {
    // Under happy-dom + DOMPurify >= 3.4.x this passes straight through.
    const out = DOMPurify.sanitize('<div>ok</div><iframe src="evil"></iframe>');
    expect(out).not.toContain("<iframe");
    expect(out).toContain("ok");
  });

  it("does not let content AFTER the first element escape sanitization", () => {
    // The specific happy-dom failure: the walk dies on the first node, so
    // everything downstream is emitted untouched.
    const out = DOMPurify.sanitize(
      '<b>first</b><img src=x onerror="alert(1)"><script>alert(2)</script>',
    );
    expect(out).not.toContain("onerror");
    expect(out).not.toContain("<script");
  });

  it("strips javascript: URLs", () => {
    const out = DOMPurify.sanitize('<a href="javascript:alert(1)">click</a>');
    expect(out).not.toContain("javascript:");
  });
});
