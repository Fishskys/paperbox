import { test as base } from "@playwright/test";

export { expect } from "@playwright/test";

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
    await page.route("**/api/jobs/queue", (route) => route.fulfill({ json: {
      started: true, concurrency: 2, running: 0, queued: 0, queued_high: 0, queued_low: 0,
    } }));
    await page.route("**/api/auth/context", (route) =>
      route.fulfill({ json: identity("admin") }),
    );
    await use(page);
  },
});
