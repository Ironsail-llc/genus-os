/**
 * The route-level error and not-found pages, in the built app.
 *
 * `/qa/route-error` throws while rendering, but only when the server runs with
 * GENUS_ENVIRONMENT=test (as playwright.config.ts starts it); anywhere else it
 * is a plain 404. A thrown route must land on the dashboard's own error page,
 * with Retry and Report, not on a blank window.
 */
import { test, expect } from "@playwright/test";

test("a route that throws renders the error page with Retry and Report", async ({ page }) => {
  const reports: string[] = [];
  await page.route("**/api/dashboard/error", async (route) => {
    reports.push(route.request().postData() ?? "");
    await route.fulfill({ status: 204 });
  });

  await page.goto("/qa/route-error");
  const notice = page.getByTestId("route-error");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText(/failed/i);
  await expect(page.getByRole("button", { name: /retry/i })).toBeVisible();

  await page.getByRole("button", { name: /report/i }).click();
  await expect.poll(() => reports.length).toBe(1);
  expect(JSON.parse(reports[0]).source).toBe("route");
});

test("an unknown path renders the not-found page with a way home", async ({ page }) => {
  const response = await page.goto("/no-such-page-anywhere");
  expect(response?.status()).toBe(404);
  await expect(page.getByTestId("not-found")).toBeVisible();
  await expect(page.getByRole("link", { name: /back/i })).toHaveAttribute("href", "/");
});
