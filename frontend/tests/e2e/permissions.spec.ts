import { expect, test } from "@playwright/test";
import { identity } from "./fixtures";

for (const role of ["read", "write", "admin"] as const) {
  test(`${role} sees only its permitted modules and reading stays read-only`, async ({
    page,
  }) => {
    await page.route("**/api/auth/context", (route) =>
      route.fulfill({ json: identity(role) }),
    );
    await page.route("**/api/papers?*", (route) =>
      route.fulfill({ json: { papers: [], total: 0 } }),
    );
    await page.goto("/library");
    await expect(
      page.getByRole("heading", { name: "常规检索", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "导入论文", exact: true }),
    ).toHaveCount(0);
    await expect(page.locator(".sidebar .nav-group-title")).toHaveText(
      role === "read"
        ? ["检索", "阅读"]
        : role === "write"
          ? ["检索", "阅读", "导入", "论文管理"]
          : ["检索", "阅读", "导入", "论文管理", "系统管理"],
    );
    if (role !== "admin") {
      await expect(
        page.locator(".sidebar a[href='/system/status']"),
      ).toHaveCount(0);
      await page.goto("/system/data");
      await expect(page).toHaveURL(/\/search$/);
      await page.goto("/system/index");
      await expect(page).toHaveURL(/\/search$/);
      await page.goto("/system/metadata");
      await expect(page).toHaveURL(/\/search$/);
      await page.goto("/system/cleanup");
      await expect(page).toHaveURL(/\/search$/);
      await expect(
        page.getByRole("heading", { name: "智能检索", exact: true }),
      ).toBeVisible();
    }
    if (role === "read") {
      await expect(page.locator('.sidebar a[href="/manage/jobs"]')).toHaveCount(0);
      await page.goto("/manage/jobs");
      await expect(page).toHaveURL(/\/search$/);
      await page.goto("/imports/jobs?job=private-job");
      await expect(page).toHaveURL(/\/search$/);
      await page.goto("/imports/metadata");
      await expect(page).toHaveURL(/\/search$/);
      await expect(page.locator("input[type=file]")).toHaveCount(0);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "打开导航" }).click();
    await expect(
      page
        .getByRole("dialog")
        .getByRole("link", { name: "论文导入", exact: true }),
    ).toHaveCount(role === "read" ? 0 : 1);
    await expect(
      page
        .getByRole("dialog")
        .getByRole("link", { name: "运行状态", exact: true }),
    ).toHaveCount(role === "admin" ? 1 : 0);
  });
}

test("credential downgrade exits protected pages and clearing it restores the proxy identity", async ({
  page,
}) => {
  await page.route("**/api/auth/context", (route) =>
    route.fulfill({
      json: identity(
        route.request().headers().authorization ? "read" : "admin",
      ),
    }),
  );
  await page.goto("/imports/metadata");
  await expect(
    page.getByRole("heading", { name: "元数据导入", exact: true }),
  ).toBeVisible();
  await page.locator("input[type=file]").setInputFiles({
    name: "private.json",
    mimeType: "application/json",
    buffer: Buffer.from("[]"),
  });
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill("test-read-key");
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.locator(".sidebar .nav-group-title")).toHaveText([
    "检索",
    "阅读",
  ]);
  await expect(page.getByText("private.json", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await expect(page.getByText(/当前权限：只读/)).toBeVisible();
  await page.getByRole("button", { name: "清除凭证", exact: true }).click();
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page.locator(".sidebar .nav-group-title")).toHaveText([
    "检索",
    "阅读",
    "导入",
    "论文管理",
    "系统管理",
  ]);
  await page.getByRole("link", { name: "元数据导入", exact: true }).click();
  await expect(
    page.getByText("尚未选择元数据文件", { exact: true }),
  ).toBeVisible();
});

test("no privileged content mounts while identity is pending or invalid", async ({
  page,
}) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let healthCalls = 0;
  await page.route("**/api/auth/context", async (route) => {
    await gate;
    await route.fulfill({ status: 403, json: { detail: "Invalid API key" } });
  });
  await page.route("**/health", (route) => {
    healthCalls++;
    return route.fulfill({ json: {} });
  });
  await page.goto("/system/status");
  await expect(page.getByText("正在验证权限…", { exact: true })).toBeVisible();
  expect(healthCalls).toBe(0);
  await expect(page.locator(".sidebar .nav-group-title")).toHaveCount(0);
  release();
  await expect(page.getByText(/Invalid API key/)).toBeVisible();
  expect(healthCalls).toBe(0);
  await expect(
    page.getByRole("button", { name: "连接与外观设置" }),
  ).toBeVisible();
});
