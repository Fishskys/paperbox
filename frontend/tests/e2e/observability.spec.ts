import { readFile } from "node:fs/promises";
import { expect, test } from "./fixtures";
import { contrastFailures } from "./contrast";

const sample = { rss_bytes: 536870912, pss_bytes: null, processes: 3, partial: false, source: "container", container: "paperbox-postgres", reason: null };
const logs = Array.from({ length: 55 }, (_, index) => ({ id: String(index), timestamp: "2026-11-06T02:00:00Z", level: "ERROR", logger: "app.worker", request_id: `r${index}`, key_prefix: "admin", message: `failed ${index}\nTraceback:\n  failure details` }));

for (const width of [1440, 390]) {
  test(`status cards and log filters work at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    let probes = 0;
    await page.route("**/health", route => {
      probes++;
      return route.fulfill({ json: { version: "test", degraded: false, services: { postgres: "ok", opensearch: "ok", embedding: "ok", rerank: "ok", minio: "ok", docling: "disabled" }, dependencies: {} } });
    });
    await page.route("**/api/admin/observability/memory", route => route.fulfill({ json: {
      measured_at: "2026-11-06T02:00:00Z", backend: { ...sample, source: "backend", container: null },
      dependencies: { postgres: sample, opensearch: { ...sample, partial: true }, embedding: { ...sample, rss_bytes: null, reason: "远程依赖，未采集本地内存" } },
    } }));
    const calls: URL[] = [];
    await page.route("**/api/admin/observability/logs**", route => {
      const url = new URL(route.request().url()); calls.push(url);
      const exporting = url.pathname.endsWith("/export");
      const offset = Number(url.searchParams.get("offset") || "0");
      const filtered = url.searchParams.get("q") ? logs.filter(entry => entry.message.includes(url.searchParams.get("q")!)) : logs;
      return route.fulfill({ json: { entries: exporting ? filtered : filtered.slice(offset, offset + 50), total: filtered.length, offset, limit: exporting ? filtered.length : 50,
        available: true, reason: null, truncated: true, scanned_bytes: 8388608, scan_limit_bytes: 8388608, measured_at: "2026-11-06T02:00:00Z" } });
    });
    await page.goto("/system/status");
    await expect(page).toHaveURL(/\/system\/observability$/);
    await expect(page.getByRole("heading", { name: "数据与日志" })).toBeVisible();
    await expect(page.locator(".system-metric-card")).toHaveCount(7);
    await expect(page.getByText("部分进程未采集")).toBeVisible();
    await expect(page.getByText("远程依赖，未采集本地内存")).toBeVisible();
    const columns = await page.locator(".system-status-grid").evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length);
    expect(columns).toBe(width === 1440 ? 3 : 1);
    await page.screenshot({ path: testInfo.outputPath("status.png"), fullPage: true });
    await page.getByRole("button", { name: "刷新状态" }).click();
    await expect.poll(() => probes).toBe(2);
    await page.getByRole("tab", { name: "日志", exact: true }).click();
    await expect(page).toHaveURL(/tab=logs/);
    await expect(page.getByText(/未覆盖全部历史/)).toBeVisible();
    await page.getByRole("listitem", { name: "2", exact: true }).click();
    await expect.poll(() => calls.at(-1)?.searchParams.get("offset")).toBe("50");
    await page.getByLabel("关键词", { exact: true }).fill("failed");
    await page.getByLabel("模块", { exact: true }).fill("app.worker");
    await page.getByLabel("请求 ID", { exact: true }).fill("r1");
    await page.getByLabel("开始时间").fill("2026-11-06T00:00");
    await page.getByLabel("结束时间").fill("2026-11-07T00:00");
    await page.getByLabel("级别", { exact: true }).click();
    await page.getByTitle("ERROR", { exact: true }).click();
    await page.getByRole("button", { name: "查询", exact: true }).click();
    await expect.poll(() => calls.at(-1)?.searchParams.get("logger")).toBe("app.worker");
    const last = calls.at(-1)!;
    expect(last.searchParams.get("level")).toBe("ERROR");
    expect(last.searchParams.get("request_id")).toBe("r1");
    expect(last.searchParams.get("since")).toBe(await page.evaluate(() => new Date("2026-11-06T00:00").toISOString()));
    expect(last.searchParams.get("offset")).toBe("0");
    await page.locator(".ant-table-row-expand-icon").first().click();
    await expect(page.getByText(/failure details/)).toBeVisible();
    const downloaded = page.waitForEvent("download");
    await page.getByRole("button", { name: "导出筛选结果" }).click();
    const download = await downloaded;
    const report = JSON.parse(await readFile((await download.path())!, "utf8"));
    expect(report.entries).toHaveLength(55);
    expect(report.truncated).toBe(true);
    expect(calls.at(-1)?.pathname).toMatch(/\/export$/);
    await page.getByRole("button", { name: "重置", exact: true }).click();
    await expect(page.getByLabel("关键词", { exact: true })).toHaveValue("");
    await page.screenshot({ path: testInfo.outputPath("logs.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.addStyleTag({ content: "*,*::before,*::after{animation:none!important;transition:none!important}" });
    expect(await contrastFailures(page)).toEqual([]);
    await page.reload();
    await expect(page.getByRole("tab", { name: "日志", exact: true })).toHaveAttribute("aria-selected", "true");
    expect(probes).toBe(2);
  });
}

test("unconfigured logs and export errors are explicit", async ({ page }) => {
  await page.route("**/api/admin/observability/logs**", route => route.fulfill({ json: {
    entries: [], total: 0, offset: 0, limit: 50, available: false, reason: "未配置应用文件日志（LOG_FILE），暂无可读取日志", truncated: false,
    scanned_bytes: 0, scan_limit_bytes: 8388608, measured_at: "2026-11-06T02:00:00Z",
  } }));
  await page.goto("/system/observability?tab=logs");
  await expect(page.getByText(/未配置应用文件日志/)).toBeVisible();
  await expect(page.getByRole("button", { name: "导出筛选结果" })).toBeDisabled();
  await page.route("**/api/admin/observability/logs**", route => route.fulfill({ status: 500, json: { detail: "读取失败" } }));
  await page.getByRole("button", { name: "刷新日志" }).click();
  await expect(page.getByText("读取失败", { exact: true })).toBeVisible();
  await page.getByLabel("开始时间").fill("2026-11-07T00:00");
  await page.getByLabel("结束时间").fill("2026-11-06T00:00");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await expect(page.getByText("开始时间不能晚于结束时间", { exact: true })).toBeVisible();
});
