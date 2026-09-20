import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page, request }) => {
  expect((await request.post("/uat/reset")).ok()).toBeTruthy();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Local acceptance workspace" })).toBeVisible();
});

test("parent pause and cancel propagate to child goals", async ({ page }) => {
  await page.getByRole("button", { name: /^Prepare customer follow-up/ }).click();
  const details = page.getByRole("region", { name: "Goal details" });
  await details.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(details.getByRole("button", { name: "Review tomorrow's customer reply · paused" })).toBeVisible();
  await expect(details.getByText(/Already dispatched external requests may finish/)).toBeVisible();
  await details.getByRole("button", { name: "Cancel goal" }).click();
  await expect(details.getByRole("button", { name: "Review tomorrow's customer reply · canceled" })).toBeVisible();
});

test("milestones require explicit authorization and share the family", async ({ page }) => {
  await page.getByRole("button", { name: /^Prepare customer follow-up/ }).click();
  const details = page.getByRole("region", { name: "Goal details" });
  await details.getByText("Add an execution milestone", { exact: true }).click();
  await page.getByLabel("Milestone objective", { exact: true }).fill("Check synthetic delivery");
  await page.getByLabel("Milestone criteria", { exact: true }).fill("Operator reviews the receipt");
  await expect(details.getByRole("button", { name: /^Check synthetic delivery/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Authorize milestone" }).click();
  await details.getByRole("button", { name: "Check synthetic delivery · queued" }).click();
  await expect(details.getByRole("button", { name: "Open parent goal" })).toBeVisible();
  await details.getByRole("button", { name: "Open parent goal" }).click();
  await expect(details.getByText("Remaining family budget: 2,000 tokens")).toBeVisible();
});

test("revised criteria invalidate old evidence and require a reason", async ({ page }) => {
  await page.getByRole("button", { name: /^Review the synthetic draft/ }).click();
  const details = page.getByRole("region", { name: "Goal details" });
  await expect(details.getByText(/Agent assessed:/)).toBeVisible();
  await details.getByText("Revise objective or criteria", { exact: true }).click();
  await page.getByLabel("Replacement criteria", { exact: true }).fill("New destination reviewed by operator");
  await page.getByLabel("Reason for revision", { exact: true }).fill("The operator changed the destination");
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(details.getByText("New destination reviewed by operator")).toBeVisible();
  await expect(details.getByText(/Agent assessed:/)).toHaveCount(0);
  await expect(details.getByRole("button", { name: "Approve completion" })).toHaveCount(0);
});

test("judgment-based completion waits for operator approval", async ({ page }) => {
  await page.getByRole("button", { name: /^Review the synthetic draft/ }).click();
  const details = page.getByRole("region", { name: "Goal details" });
  await expect(details.getByText(/^review ·/)).toBeVisible();
  await details.getByRole("button", { name: "Approve completion" }).click();
  await expect(details.getByText(/^complete ·/)).toBeVisible();
  await expect(details.getByText("Start pursuing the objective", { exact: true })).toHaveCount(0);
  await expect(details.getByRole("button", { name: "Approve completion" })).toHaveCount(0);
  await page.screenshot({ path: "test-results/runtime-uat/approved-goal.png", fullPage: true });
});
