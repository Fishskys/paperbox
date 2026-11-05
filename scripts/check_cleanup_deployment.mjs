import { createRequire } from "node:module";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:8089";
const key = process.env.PAPERBOX_UI_TEST_KEY;
if (!key) throw new Error("Run through scripts/check_webui_deployment.py with WEBUI_CLEANUP_ONLY=1");
const output = resolve(import.meta.dirname, "../logs/codex");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/admin/cleanup/**", route => {
    const request = route.request();
    if (request.method() === "GET") return route.continue();
    if (new URL(request.url()).pathname.endsWith("/jobs") && request.postDataJSON()?.dry_run === true) return route.continue();
    return route.fulfill({ status: 409, json: { detail: "Deployment acceptance only previews cleanup" } });
  });
  await page.goto(origin);
  const html = await readFile(resolve(import.meta.dirname, "../frontend/dist/index.html"), "utf8");
  const expected = await page.evaluate(value => new DOMParser().parseFromString(value, "text/html").querySelector('script[type="module"][src]')?.getAttribute("src"), html);
  if (process.env.WEBUI_SKIP_BUNDLE_CHECK !== "1") {
    expect(await page.locator('script[type="module"][src]').getAttribute("src")).toBe(expected);
  }
  async function authenticate() {
    await page.getByRole("button", { name: "连接与外观设置", exact: true }).click();
    await page.getByLabel("API key", { exact: true }).fill(key);
    await page.getByRole("button", { name: /^应\s*用$/ }).click();
    await expect(page.getByRole("link", { name: "系统清理", exact: true })).toBeVisible();
  }
  await authenticate();
  const headers = { Authorization: `Bearer ${key}` };
  const before = await (await page.request.get(`${origin}/api/papers?limit=1`, { headers })).json();
  await page.getByRole("link", { name: "系统清理", exact: true }).click();
  await expect(page.getByRole("heading", { name: "系统清理", exact: true })).toBeVisible();
  const policy = await page.request.get(`${origin}/api/admin/cleanup/policy`, { headers });
  expect(policy.ok()).toBeTruthy();
  await page.getByRole("tab", { name: "清理预览", exact: true }).click();
  const submitted = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/admin/cleanup/jobs");
  await page.getByRole("button", { name: "预览清理", exact: true }).click();
  const accepted = await submitted;
  expect(accepted.status()).toBe(202);
  const job = await accepted.json();
  await expect.poll(async () => (await (await page.request.get(`${origin}/api/admin/cleanup/jobs/${job.job_id}`, { headers })).json()).stage,
    { timeout: 120000 }).toBe("COMPLETED");
  const report = await page.request.get(`${origin}/api/admin/cleanup/jobs/${job.job_id}/download`, { headers });
  expect(report.ok()).toBeTruthy();
  expect((await report.json()).dry_run).toBe(true);
  await page.reload();
  await authenticate();
  await page.getByRole("link", { name: "系统清理", exact: true }).click();
  await expect(page.getByText(`任务 ${job.job_id}`, { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "下载完整清理报告", exact: true })).toBeVisible();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: resolve(output, `cleanup-deployed-${width}.png`), fullPage: true });
  }
  const after = await (await page.request.get(`${origin}/api/papers?limit=1`, { headers })).json();
  expect(after.total).toBe(before.total);
  expect(errors).toEqual([]);
  console.info(`Cleanup deployment passed: bundle ${process.env.WEBUI_SKIP_BUNDLE_CHECK === "1" ? "check skipped for development" : "matched"}, administrator policy, queued preview, reload authentication, report recovery and desktop/mobile layout; no deletion requested.`);
} catch (error) {
  process.stderr.write(String(error).replaceAll(key, "[redacted]") + "\n");
  process.exitCode = 1;
} finally {
  await browser.close();
}