import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Safe default for every spec: no session, no role. Components (e.g.
// ControlsView) that call `useSession()` would otherwise throw
// "`useSession` must be wrapped in a <SessionProvider />" outside the real
// app tree (only `src/app/layout.tsx` renders one). Specs that care about a
// specific role (see controls-view.test.tsx) declare their own
// `vi.mock("next-auth/react", ...)`, which takes precedence for that file.
vi.mock("next-auth/react", () => ({
  useSession: () => ({ data: null, status: "unauthenticated", update: vi.fn() }),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
}));

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
