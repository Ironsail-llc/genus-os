import "@testing-library/jest-dom/vitest";

// jsdom does not implement scrollIntoView (happy-dom did). The suite runs under
// jsdom because that is what production's isomorphic-dompurify uses — and because
// under happy-dom, DOMPurify >= 3.4.x silently degrades to a NO-OP while still
// reporting isSupported === true. See canvas/__tests__/sanitization.test.ts.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () {};
}

// jsdom does not implement matchMedia either. Report "does not match" so the
// responsive layout resolves to its desktop default in tests.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
