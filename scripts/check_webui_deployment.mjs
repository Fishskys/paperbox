import { createRequire } from "node:module";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expandNavigation } from "./webui-navigation.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:8089";
const key = process.env.PAPERBOX_UI_TEST_KEY;
if (!key) throw new Error("Use scripts/check_webui_deployment.py for a temporary credential");
const output = resolve(import.meta.dirname, "../logs/codex");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
let page;
const errors = [];
try {
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(20000);
  await page.route("**/api/maintenance/jobs", route => {
    const body = route.request().postDataJSON();
    if (body.dry_run === false) return route.fulfill({ status: 409, json: { detail: "Deployment acceptance only previews maintenance" } });
    return route.continue();
  });
  await page.route("**/api/metadata/sources/**", (route) => {
    const request = route.request();
    if (request.method() === "GET") return route.continue();
    if (new URL(request.url()).pathname.endsWith("/rematch") && request.postDataJSON()?.apply === false) return route.continue();
    return route.fulfill({ status: 409, json: { detail: "Deployment acceptance only previews source attribution" } });
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/library`);
  const localIndex = await readFile(resolve(import.meta.dirname, "../frontend/dist/index.html"), "utf8");
  const expectedBundle = await page.evaluate((html) => new DOMParser().parseFromString(html, "text/html").querySelector('script[type="module"][src]')?.getAttribute("src"), localIndex);
  const servedBundle = await page.locator('script[type="module"][src]').getAttribute("src");
  if (!expectedBundle || servedBundle !== expectedBundle) throw new Error(`Stale deployment bundle: expected ${expectedBundle}, got ${servedBundle}`);
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page.locator(".sidebar .nav-group-title")).toHaveCount(5);
  const baseline = await page.evaluate(async () => (await (await fetch("/api/papers?limit=1")).json()).total);
  await expandNavigation(page, "检索");
  await page.getByRole("link", { name: "常规检索", exact: true }).click();
  await expect(page.getByRole("heading", { name: "常规检索", exact: true })).toBeVisible();
  await expect(page.locator(".ant-table-tbody .paper-cell a").first()).toBeVisible();
  await page.screenshot({ path: resolve(output, "webui-deployed-catalog.png") });
  await expandNavigation(page, "导入");
  await page.getByRole("link", { name: "元数据导入", exact: true }).click();
  await page.locator("input[type=file]").setInputFiles(resolve(import.meta.dirname, "../tests/metadata/ieee_metadata_example.jsonl"));
  const preview = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/metadata/import");
  await page.getByRole("button", { name: "预览导入" }).click();
  const previewResponse = await preview;
  if (!previewResponse.ok()) throw new Error(`JSONL preview failed: ${previewResponse.status()}`);
  await page.screenshot({ path: resolve(output, "webui-deployed-metadata.png") });
  await page.getByRole("tab", { name: "元数据复核", exact: true }).click();
  const beforeReview = await page.evaluate(async () => (await (await fetch("/api/metadata/review?limit=200")).json()));
  let sourcePreview = null;
  if (beforeReview.items.length) {
    await page.getByRole("button", { name: "查看与处理", exact: true }).first().click();
    const sourceDrawer = page.getByRole("dialog", { name: "来源详情", exact: true });
    await expect(sourceDrawer.getByRole("heading", { name: "当前匹配结果", exact: true })).toBeVisible();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect.poll(() => sourceDrawer.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        return bounds.left >= -1 && bounds.right <= innerWidth + 1;
      })).toBe(true);
      await page.screenshot({ path: resolve(output, `webui-deployed-source-${width}.png`) });
    }
    await sourceDrawer.getByRole("button", { name: "拒绝来源", exact: true }).click();
    const reject = page.getByRole("dialog", { name: "拒绝来源", exact: true });
    await expect(reject.locator("input, textarea")).toHaveCount(0);
    await reject.getByRole("button", { name: /^取\s*消$/ }).click();
    await sourceDrawer.locator(".ant-drawer-close").click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("checkbox", { name: "Select all", exact: true }).check();
    for (let offset = 20; beforeReview.total <= 50 && offset < beforeReview.total; offset += 20) {
      const sourceJumper = page.locator(".ant-pagination-options-quick-jumper input").first();
      await sourceJumper.fill(String(offset / 20 + 1)); await sourceJumper.press("Enter");
      await expect(page.getByText(beforeReview.items[offset].source_ref, { exact: true })).toBeVisible();
      await page.getByRole("checkbox", { name: "Select all", exact: true }).check();
    }
    const rematch = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/metadata/sources/rematch", { timeout: 120000 });
    await page.getByRole("button", { name: "重新匹配所选", exact: true }).click();
    const rematchResponse = await rematch;
    const sourceReport = await rematchResponse.json();
    if (!rematchResponse.ok() || sourceReport.applied !== 0) throw new Error("Invalid source review preview");
    if (beforeReview.total <= 50 && sourceReport.items.length !== beforeReview.total) throw new Error("Cross-page selection lost source records");
    sourcePreview = { checked: sourceReport.items.length,
      matched: sourceReport.items.filter((item) => item.match?.match_status === "matched").length };
    const batch = page.getByRole("dialog", { name: "批量重新匹配", exact: true });
    await expect(batch.getByRole("button", { name: /应用唯一匹配/ })).toBeVisible();
    await page.screenshot({ path: resolve(output, "webui-deployed-source-batch.png") });
    await batch.locator(".ant-modal-footer").getByRole("button", { name: /^关\s*闭$/ }).click();
    const afterReview = await page.evaluate(async () => (await (await fetch("/api/metadata/review?limit=200")).json()));
    if (JSON.stringify(beforeReview) !== JSON.stringify(afterReview)) throw new Error("Source preview changed the review queue");
  }
  if (process.env.WEBUI_SOURCE_ONLY === "1") {
    if (errors.length) throw new Error(errors.join("\n"));
    process.stdout.write(JSON.stringify({ origin, bundle: servedBundle, status: "passed", sourcePreview, output }) + "\n");
  } else {
  await expandNavigation(page, "系统管理");
  await page.getByRole("link", { name: "索引维护", exact: true }).click();
  const papers = await page.evaluate(async () => (await (await fetch("/api/papers?limit=1")).json()).papers);
  await page.getByLabel("维护论文 ID").fill(papers[0].paper_id);
  const indexPreview = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/papers/reindex", { timeout: 180000 });
  void indexPreview.catch(() => {});
  await page.getByRole("button", { name: "预览维护" }).click();
  const indexReport = await (await indexPreview).json();
  if (!indexReport.dry_run || indexReport.queued !== 0 || indexReport.selected !== 1) throw new Error("Invalid index preview");
  await expect(page.getByRole("button", { name: "执行维护" })).toBeEnabled();
  await page.getByRole("link", { name: "元数据维护", exact: true }).click();
  await page.getByLabel("元数据处理上限").fill("1");
  const metadataPreview = page.waitForResponse((response) => /\/api\/maintenance\/jobs\/[^/]+\/report$/.test(new URL(response.url()).pathname), { timeout: 180000 });
  await page.getByRole("button", { name: "预览维护" }).click();
  const metadataReport = await (await metadataPreview).json();
  if (!metadataReport.dry_run || metadataReport.papers !== 1) throw new Error("Invalid metadata preview");
  await page.getByRole("link", { name: "数据检查", exact: true }).click();
  const audit = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/consistency/doi");
  await page.getByRole("button", { name: "检查标识" }).click();
  if (!(await audit).ok()) throw new Error("Identifier audit failed");
  const consistency = page.waitForResponse((response) => /\/api\/maintenance\/jobs\/[^/]+\/report$/.test(new URL(response.url()).pathname), { timeout: 600000 });
  void consistency.catch(() => {});
  await page.getByRole("button", { name: "开始检查" }).click();
  await expect(page).toHaveURL(/\/system\/data\?task=[a-f0-9-]+/);
  const consistencyTask = new URL(page.url()).searchParams.get("task");
  await page.reload();
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page.locator(".sidebar .nav-group-title")).toHaveCount(5);
  await expandNavigation(page, "系统管理");
  await page.getByRole("link", { name: "数据检查", exact: true }).click();
  await expect(page.getByText(new RegExp(`任务 ${consistencyTask}`))).toBeVisible();
  const consistencyResponse = await consistency;
  if (!consistencyResponse.ok()) throw new Error(`Consistency check failed: ${consistencyResponse.status()}`);
  const consistencyReport = await consistencyResponse.json();
  await page.screenshot({ path: resolve(output, "webui-deployed-system.png") });
  await expandNavigation(page, "检索");
  await page.getByRole("link", { name: "常规检索", exact: true }).click();
  await page.locator(".ant-table-tbody .paper-cell a").first().click();
  const canvas = page.locator(".pdf-sheet canvas").first();
  await expect(canvas).toBeVisible({ timeout: 30000 });
  async function verifyCanvas() {
    await expect.poll(() => canvas.evaluate((element) => {
      if (element.getBoundingClientRect().width > innerWidth - 24) return 0;
      const pixels = element.getContext("2d").getImageData(0, 0, element.width, element.height).data;
      let ink = 0;
      for (let i = 0; i < pixels.length; i += 40) if (pixels[i] < 180 && pixels[i + 3]) ink++;
      return ink;
    })).toBeGreaterThan(100);
  }
  await verifyCanvas();
  await page.getByRole("button", { name: "阅读导航", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "阅读导航", exact: true });
  await drawer.getByText("缩略图", { exact: true }).click();
  await expect(drawer.locator("canvas").first()).toBeVisible();
  await drawer.getByText("附图", { exact: true }).click();
  await page.screenshot({ path: resolve(output, "webui-deployed-reader-navigation.png") });
  await drawer.locator(".ant-drawer-close").click();
  await expect(drawer).not.toBeVisible();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await verifyCanvas();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: resolve(output, `webui-deployed-reader-${width}.png`) });
  }
  const total = await page.evaluate(async () => (await (await fetch("/api/papers?limit=1")).json()).total);
  if (total !== baseline) throw new Error(`Read/preview operations changed paper total: ${baseline} -> ${total}`);
  const figurePaper = process.env.PAPERBOX_UI_FIGURE_PAPER;
  if (figurePaper) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${origin}/reading/papers/${encodeURIComponent(figurePaper)}`);
    await page.getByRole("button", { name: "连接与外观设置" }).click();
    await page.getByLabel("API key", { exact: true }).fill(key);
    await page.getByRole("button", { name: /^应\s*用$/ }).click();
    await expect(page.getByRole("button", { name: "阅读导航", exact: true })).toBeEnabled({ timeout: 30000 });
    await page.getByRole("button", { name: "阅读导航", exact: true }).click();
    const figureDrawer = page.getByRole("dialog", { name: "阅读导航", exact: true });
    await figureDrawer.getByText("附图", { exact: true }).click();
    const image = figureDrawer.locator(".figure-preview").first();
    await expect(image).toBeVisible({ timeout: 30000 });
    await expect.poll(() => image.evaluate((element) => element.naturalWidth)).toBeGreaterThan(0);
    const download = page.waitForEvent("download");
    await figureDrawer.getByRole("button", { name: "下载附图", exact: true }).first().click();
    if (!(await download).suggestedFilename().endsWith(".pdf")) throw new Error("Native figure is not PDF");
    await page.screenshot({ path: resolve(output, "webui-deployed-figures.png") });
  }
  if (JSON.stringify(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).includes(key)) throw new Error("Credential persisted");
  if (errors.length) throw new Error(errors.join("\n"));
  process.stdout.write(JSON.stringify({ origin, bundle: servedBundle, status: "passed", papers: total, indexPreview: indexReport.selected, metadataPreview: metadataReport.papers,
    consistency: { consistent: consistencyReport.consistent, problems: consistencyReport.totals.problems, errors: consistencyReport.errors, restoredAfterReload: true }, nativeFigures: !!figurePaper, output }) + "\n");
  }
} catch (error) {
  if (page && !page.isClosed()) {
    await page.screenshot({ path: resolve(output, "webui-deployment-failure.png"), fullPage: true }).catch(() => {});
    process.stderr.write(JSON.stringify({ url: page.url(), pageErrors: errors, alerts: await page.getByRole("alert").allTextContents().catch(() => []) }).replaceAll(key, "[redacted]") + "\n");
  }
  process.stderr.write(String(error).replaceAll(key, "[redacted]") + "\n");
  process.exitCode = 1;
} finally { await browser.close(); }
