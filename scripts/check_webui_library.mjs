import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { performance } from "node:perf_hooks";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const output = resolve(import.meta.dirname, "../logs/codex");
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.route("**/api/**", (route) => ["GET", "HEAD"].includes(route.request().method())
    ? route.continue()
    : route.fulfill({ status: 409, json: { detail: "Live library inspection is read-only" } }));
  await page.goto(`${origin}/manage/library`);
  const rows = page.locator(".ant-table-tbody .ant-table-row");
  await expect(rows.first()).toBeVisible();
  const pending = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/papers" && url.searchParams.get("status") === "INDEXED";
  });
  const started = performance.now();
  await page.getByRole("tab", { name: "已索引", exact: true }).click();
  const response = await pending;
  if (!response.ok()) throw new Error(`Indexed list unavailable: HTTP ${response.status()}`);
  const report = await response.json();
  if (!report.papers.length || report.papers.some((paper) => paper.status !== "INDEXED"))
    throw new Error("Indexed filter returned incorrect paper states");
  await expect(rows).toHaveCount(report.papers.length);
  await expect(page.locator(".ant-spin-spinning")).toHaveCount(0);
  const clickToRows = Math.round(performance.now() - started);
  await expect(page).toHaveURL(/status=INDEXED/);
  const timing = response.request().timing();
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: resolve(output, "webui-library-indexed-live.png"), fullPage: true });
  await page.reload();
  await expect(rows).toHaveCount(report.papers.length);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".management-status")).toBeVisible();
  await expect(page.getByRole("tab", { name: "已索引", exact: true })).toHaveAttribute("aria-selected", "true");
  if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)))
    throw new Error("Management layout overflows on mobile");
  await page.screenshot({ path: resolve(output, "webui-library-indexed-live-390.png"), fullPage: true });
  console.log(JSON.stringify({ status: "passed", filter: "INDEXED", total: report.total,
    returned: report.papers.length, http_ms: Math.round(timing.responseEnd),
    click_to_rows_ms: clickToRows, refresh: "verified", viewport: "desktop/mobile" }));
} finally {
  await browser.close();
}