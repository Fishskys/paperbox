import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { mkdir, unlink } from "node:fs/promises";
import { resolve } from "node:path";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const config = JSON.parse(process.env.PAPERBOX_FIGURE_TEST || "{}");
if (!config.paper_id || !config.keys?.write || !config.keys?.read || !config.python)
  throw new Error("Run through scripts/check_webui_figures.py");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const paperId = config.paper_id;
const base = `/api/papers/${paperId}/figures`;
const output = resolve(import.meta.dirname, "../logs/codex");
await mkdir(output, { recursive: true });
const zipPath = resolve(output, `webui-figures-${paperId}.zip`);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  // Mutations in this acceptance run are limited to the disposable paper.
  await page.route("**/api/**", route => ["GET", "HEAD"].includes(route.request().method()) ||
    new URL(route.request().url()).pathname.startsWith(`/api/papers/${paperId}/`)
    ? route.continue() : route.fulfill({ status: 403, json: { detail: "Acceptance write target rejected" } }));
  await page.goto(`${origin}/search/standard`);
  async function role(name) {
    await page.getByRole("button", { name: "连接与外观设置" }).click();
    await page.getByLabel("API key", { exact: true }).fill(config.keys[name]);
    const response = page.waitForResponse(r => new URL(r.url()).pathname === "/api/auth/context");
    await page.getByRole("button", { name: /^应\s*用$/ }).click();
    expect((await (await response).json()).role).toBe(name);
  }
  async function management() {
    await page.getByRole("link", { name: "论文库管理", exact: true }).click();
    const title = `WebUI figure acceptance ${paperId}`;
    await page.getByLabel("快速定位论文", { exact: true }).fill(title);
    await page.getByLabel("快速定位论文", { exact: true }).press("Enter");
    await page.getByRole("button", { name: `管理论文 ${title}`, exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
    await drawer.getByRole("tab", { name: "附图", exact: true }).click();
    return drawer;
  }
  await role("write");
  let drawer = await management();
  await expect(drawer.getByRole("button", { name: "下载全部附图", exact: true })).toBeDisabled();
  await drawer.getByRole("button", { name: "提取附图", exact: true }).click();
  await expect(drawer.locator(".paper-figure").first()).toBeVisible({ timeout: 90000 });
  await expect(drawer.getByRole("button", { name: "重新提取附图", exact: true })).toBeEnabled({ timeout: 90000 });
  const count = await drawer.locator(".paper-figure").count();
  expect(count).toBeGreaterThan(0);
  await expect(drawer.locator(".figure-preview").first()).toBeVisible();
  await expect.poll(() => drawer.locator(".figure-preview").first().evaluate(img => img.naturalWidth)).toBeGreaterThan(0);
  await page.screenshot({ path: resolve(output, "webui-figures-management-1440.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(async () => {
    const box = await drawer.boundingBox();
    return !!box && box.x >= 0 && box.width <= 390;
  }).toBe(true);
  await page.screenshot({ path: resolve(output, "webui-figures-management-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const inspect = async () => (await page.request.get(`${origin}${base}`, { headers: { Authorization: `Bearer ${config.keys.write}` } })).json();
  const before = await inspect();
  await drawer.getByRole("button", { name: "重新提取附图", exact: true }).click();
  await page.getByRole("dialog", { name: "重新提取附图", exact: true }).getByRole("button", { name: "确认执行" }).click();
  await expect.poll(async () => (await inspect()).generation_id, { timeout: 90000 }).not.toBe(before.generation_id);
  await drawer.getByRole("button", { name: "刷新附图" }).click();
  await expect(drawer.getByRole("button", { name: "重新提取附图", exact: true })).toBeEnabled();
  await drawer.getByRole("link", { name: /^第 \d+ 页$/ }).first().click();
  await role("read");
  await page.getByRole("button", { name: "阅读导航", exact: true }).click();
  const reading = page.getByRole("dialog", { name: "阅读导航", exact: true });
  await reading.getByText("附图", { exact: true }).click();
  await expect(reading.getByRole("button", { name: "重新提取附图", exact: true })).toHaveCount(0);
  await expect(reading.getByRole("button", { name: "删除全部附图", exact: true })).toHaveCount(0);
  const download = page.waitForEvent("download");
  await reading.getByRole("button", { name: "下载全部附图", exact: true }).click();
  await (await download).saveAs(zipPath);
  const manifest = JSON.parse(execFileSync(config.python, ["-c", `
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    assert archive.testzip() is None
    print(archive.read("manifest.json").decode("utf-8"))
`, zipPath], { encoding: "utf8" }));
  expect(manifest.figures.length).toBe(count);
  expect(manifest.generation_id).not.toBe(before.generation_id);
  await page.keyboard.press("Escape");
  await expect(reading).toBeHidden();
  await role("write");
  drawer = await management();
  await drawer.getByRole("button", { name: /^删除附图 / }).first().click();
  const remove = page.getByRole("dialog", { name: "删除附图", exact: true });
  await remove.getByRole("button", { name: /^取\s*消$/ }).click();
  await expect(drawer.locator(".paper-figure")).toHaveCount(count);
  await drawer.getByRole("button", { name: /^删除附图 / }).first().click();
  await remove.getByRole("button", { name: "确认执行" }).click();
  await expect(drawer.locator(".paper-figure")).toHaveCount(count - 1);
  await drawer.getByRole("button", { name: "删除全部附图", exact: true }).click();
  await remove.getByRole("button", { name: "确认执行" }).click();
  await expect(drawer.getByRole("button", { name: "下载全部附图", exact: true })).toBeDisabled();
  await expect(drawer.locator(".paper-figure")).toHaveCount(0);
  console.log(JSON.stringify({ status: "passed", figures: count, extract: true, reextract: true,
    read_zip: true, delete_preview_cancel: true, delete_single: true, delete_all: true, responsive: true }));
} finally {
  await browser.close();
  await unlink(zipPath).catch(error => { if (error.code !== "ENOENT") throw error; });
}
