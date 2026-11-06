import { test as base, type Locator, type Page } from "@playwright/test";

export { expect } from "@playwright/test";

export async function expandNavigation(page: Page, label: string, root: Locator = page.locator(".sidebar")) {
  const group = root.getByRole("button", { name: label, exact: true });
  if (await group.getAttribute("aria-expanded") !== "true") await group.click();
}

export const identity = (role: "read" | "write" | "admin") => ({
  prefix: `${role}-test`,
  role,
  source: "db",
  is_anonymous: false,
  auth_enabled: true,
  read_access: "public",
  capabilities: { read: true, write: role !== "read", admin: role === "admin" },
});

// Existing workflow tests run as an administrator without consulting the live API.
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route("**/api/admin/observability/memory", route => route.fulfill({ json: {
      measured_at: "2026-11-06T10:00:00Z",
      backend: { rss_bytes: 104857600, pss_bytes: 83886080, processes: 1, partial: false, source: "backend", container: null, reason: null },
      dependencies: {},
    } }));
    await page.route("**/api/jobs/queue", (route) => route.fulfill({ json: {
      started: true, concurrency: 2, running: 0, queued: 0, queued_high: 0, queued_low: 0,
    } }));
    await page.route("**/api/auth/context", (route) =>
      route.fulfill({ json: identity("admin") }),
    );
    await use(page);
  },
});
