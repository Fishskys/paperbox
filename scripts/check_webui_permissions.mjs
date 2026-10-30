import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const require = createRequire(
  new URL("../frontend/package.json", import.meta.url),
);
const { chromium, expect } = require("@playwright/test");
const keys = JSON.parse(process.env.PAPERBOX_UI_TEST_KEYS || "{}");
if (!["read", "write", "admin"].every((role) => keys[role]))
  throw new Error(
    "Run through scripts/check_webui_permissions.py to create temporary test keys",
  );
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const output = resolve(import.meta.dirname, "../logs/codex");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const reports = [];
  await page.goto(`${origin}/library`);
  async function applyRole(role) {
    await page.getByRole("button", { name: "连接与外观设置" }).click();
    await page.getByLabel("API key", { exact: true }).fill(keys[role]);
    const pending = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/auth/context",
    );
    await page.getByRole("button", { name: /^应\s*用$/ }).click();
    const response = await pending;
    if (!response.ok() || (await response.json()).role !== role)
      throw new Error(`Effective ${role} identity did not resolve`);
    return response;
  }
  for (const role of ["admin", "read", "write"]) {
    const response = await applyRole(role);
    const groups =
      role === "read"
        ? ["检索", "阅读"]
        : role === "write"
          ? ["检索", "阅读", "导入", "论文管理"]
          : ["检索", "阅读", "导入", "论文管理", "系统管理"];
    await expect(page.locator(".sidebar .nav-group-title")).toHaveText(groups);
    await expect(page.locator('.sidebar a[href="/library"]')).toHaveCount(0);
    await expect(page.locator('.sidebar a[href="/imports/jobs"]')).toHaveCount(0);
    await expect(page.locator('.sidebar a[href="/manage/jobs"]')).toHaveCount(role === "read" ? 0 : 1);
    await page.getByRole("link", { name: "常规检索", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "常规检索", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "导入论文", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^管理论文 / })).toHaveCount(
      0,
    );
    if (role !== "admin") {
      await page.goto(`${origin}/system/status`);
      await expect(page).toHaveURL(/\/search$/);
    }
    if (role === "read") {
      for (const path of ["/manage/library", "/imports/metadata"]) {
        await page.goto(`${origin}${path}`);
        await expect(page).toHaveURL(/\/search$/);
      }
    }
    if (role === "write") {
      await applyRole(role);
      const listing = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/api/papers",
      );
      await page.getByRole("link", { name: "论文库管理", exact: true }).click();
      if (!(await listing).ok()) throw new Error("Management list unavailable");
      await expect(
        page.getByRole("heading", { name: "论文库管理", exact: true }),
      ).toBeVisible();
      await expect(page.getByRole("columnheader", { name: "原文", exact: true })).toBeVisible();
      await expect(page.getByRole("columnheader", { name: "年份", exact: true })).toHaveCount(0);
      const indexed = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return url.pathname === "/api/papers" && url.searchParams.get("status") === "INDEXED";
      });
      await page.getByRole("tab", { name: "已索引", exact: true }).click();
      const indexedList = await (await indexed).json();
      if (indexedList.papers.some((paper) => paper.status !== "INDEXED")) throw new Error("Incorrect management status filter");
      const sample = indexedList.papers.find((paper) => paper.doi);
      if (sample) {
        await page.getByRole("combobox", { name: "定位字段" }).click();
        await page.locator(".ant-select-item-option-content").getByText("DOI", { exact: true }).click();
        await page.getByLabel("快速定位论文").fill(sample.doi);
        const located = page.waitForResponse((response) => {
          const url = new URL(response.url());
          return url.pathname === "/api/papers" && url.searchParams.get("q") === sample.doi && url.searchParams.get("scope") === "doi";
        });
        await page.getByLabel("快速定位论文").press("Enter");
        if (!(await (await located).json()).papers.some((paper) => paper.paper_id === sample.paper_id)) throw new Error("DOI locator did not find the sample paper");
      }
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: `Management list fits at ${width}` }).toBe(true);
        await page.screenshot({ path: resolve(output, `webui-management-list-${width}.png`) });
      }
      await page.setViewportSize({ width: 1440, height: 1000 });
      const manage = page.getByRole("button", { name: /^管理论文 / }).first();
      if (indexedList.total > 0) {
        await expect(manage).toBeVisible();
        await manage.click();
        const drawer = page.getByRole("dialog", {
          name: "管理论文",
          exact: true,
        });
        await expect(drawer.getByLabel("标题", { exact: true })).toBeVisible();
        for (const [width, height] of [
          [1440, 1000],
          [390, 844],
        ]) {
          await page.setViewportSize({ width, height });
          await expect
            .poll(
              async () => {
                const box = await drawer.boundingBox();
                return !!box && box.x >= 0 && box.x + box.width <= width + 1;
              },
              { message: `Management drawer fits at ${width}` },
            )
            .toBe(true);
          await page.screenshot({
            path: resolve(output, `webui-management-${width}.png`),
            fullPage: false,
          });
        }
      }
      await page.goto(`${origin}/library`);
      await page.setViewportSize({ width: 1440, height: 900 });
      await applyRole(role);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "打开导航" }).click();
    await expect(
      page
        .getByRole("dialog")
        .getByRole("link", { name: "论文库管理", exact: true }),
    ).toHaveCount(role === "read" ? 0 : 1);
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("dialog", { name: "Paperbox", exact: true }),
    ).not.toBeVisible();
    await page.setViewportSize({ width: 1440, height: 900 });
    reports.push({ role, status: response.status(), modules: groups });
  }
  console.log(JSON.stringify({ origin, status: "passed", reports }));
} finally {
  await browser.close();
}
