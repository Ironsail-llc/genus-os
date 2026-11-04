import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

type Metric = { name: string; value: number; rating: string; id: string; navigationType: string };
let reporter: ((metric: Metric) => void) | null = null;
vi.mock("next/web-vitals", () => ({
  useReportWebVitals: (cb: (metric: Metric) => void) => {
    reporter = cb;
  },
}));
vi.mock("next/font/google", () => ({
  Geist: () => ({ variable: "font-sans" }),
  Geist_Mono: () => ({ variable: "font-mono" }),
}));
vi.mock("next-auth/react", () => ({
  SessionProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/theme-provider", () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import RootLayout from "@/app/layout";

/**
 * Every page reports its vitals: the root layout always mounts the reporter,
 * whatever the environment holds. There is no off switch.
 */
describe("RootLayout web vitals", () => {
  const sendBeacon = vi.fn(() => true);

  beforeEach(() => {
    reporter = null;
    sendBeacon.mockClear();
    Object.defineProperty(navigator, "sendBeacon", { value: sendBeacon, configurable: true });
    window.history.replaceState(null, "", "/?v=chat");
  });

  afterEach(() => {
    delete process.env.GENUS_DASHBOARD_WEB_VITALS;
  });

  it.each([undefined, "false", "0", "off", "no"])(
    "mounts the reporter and beacons to /api/vitals with GENUS_DASHBOARD_WEB_VITALS=%s",
    (value) => {
      if (value === undefined) delete process.env.GENUS_DASHBOARD_WEB_VITALS;
      else process.env.GENUS_DASHBOARD_WEB_VITALS = value;

      renderToStaticMarkup(<RootLayout>page</RootLayout>);
      expect(reporter).not.toBeNull();

      reporter!({ name: "LCP", value: 900, rating: "good", id: "v1-1", navigationType: "navigate" });

      expect(sendBeacon).toHaveBeenCalledTimes(1);
      expect((sendBeacon.mock.calls[0] as unknown as [string])[0]).toBe("/api/vitals");
    }
  );
});
