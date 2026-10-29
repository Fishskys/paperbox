import { expect, test } from "@playwright/test";
import { identity } from "./fixtures";

test("refreshing an explicit credential does not restore the proxy administrator", async ({
  page,
}) => {
  await page.route("**/api/auth/context", (route) => {
    const headers = route.request().headers();
    return route.fulfill({
      json: identity(
        headers.authorization || headers["x-paperbox-auth-mode"] === "explicit"
          ? "read"
          : "admin",
      ),
    });
  });
  await page.goto("/search");
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill("refresh-read-key");
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page.locator(".sidebar .nav-group-title")).toHaveText([
    "检索",
    "阅读",
  ]);
  await page.goto("/system/status");
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.locator(".sidebar .nav-group-title")).toHaveText([
    "检索",
    "阅读",
  ]);
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("paperbox:credential-mode"),
    ),
  ).toBe("explicit");
  expect(
    await page.evaluate(() => JSON.stringify({ ...sessionStorage })),
  ).not.toContain("refresh-read-key");
});
