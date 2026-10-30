import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const output = resolve(import.meta.dirname, "../logs/codex");
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.route("**/api/**", (route) => ["GET", "HEAD"].includes(route.request().method())
    ? route.continue()
    : route.fulfill({ status: 409, json: { detail: "Live job inspection is read-only" } }));
  const recent = await page.request.get(`${origin}/api/jobs?limit=20`);
  if (!recent.ok()) throw new Error(`Jobs unavailable: HTTP ${recent.status()}`);
  const job = (await recent.json()).jobs.find((item) => item.paper_id);
  if (!job || !job.kind) throw new Error("No associated task with an explicit kind available");
  const invalid = await page.request.get(`${origin}/api/jobs?paper_id=invalid`);
  if (invalid.status() !== 422) throw new Error("Invalid paper UUID must return 422");
  await page.goto(`${origin}/manage/jobs?paper_id=${encodeURIComponent(job.paper_id)}&job=${encodeURIComponent(job.job_id)}`);
  await expect(page.getByRole("heading", { name: "处理任务", exact: true })).toBeVisible();
  await expect(page.getByLabel("按论文 ID 筛选任务", { exact: true })).toHaveValue(job.paper_id);
  await expect(page.getByLabel("任务队列状态")).toBeVisible();
  const drawer = page.getByRole("dialog", { name: "任务详情", exact: true });
  await expect(drawer.getByText(job.job_id, { exact: true })).toBeVisible();
  await expect(drawer.getByText("总耗时（含排队）", { exact: true })).toBeVisible();
  await expect(drawer.getByRole("link", { name: "管理论文", exact: true }))
    .toHaveAttribute("href", `/manage/library?paper=${encodeURIComponent(job.paper_id)}`);
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: resolve(output, "webui-jobs-live.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => {
    const box = await drawer.boundingBox();
    return !!box && box.x >= 0 && box.x + box.width <= 391;
  }).toBe(true);
  await page.screenshot({ path: resolve(output, "webui-jobs-live-390.png"), fullPage: true });
  console.log(JSON.stringify({ status: "passed", kind: job.kind, paper_filter: "verified", queue: "visible", invalid_uuid: 422, viewport: "desktop/mobile" }));
} finally {
  await browser.close();
}
