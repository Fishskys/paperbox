import { expect, test } from "./fixtures";
import { contrastFailures } from "./contrast";

test("cleanup preview survives reload, confirms execution and hides destructive work from automation", async ({ page }) => {
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/admin/cleanup/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/policy")) return route.fulfill({ json: { automatic: false, interval_s: 3600, preview_hours: 24, batch_size: 100,
      days: { app_logs: 30, search_logs: 90, jobs: 90 }, log_rotation: { enabled: false, max_bytes: 52428800, backups: 5 },
      upload_gc: { interval_s: 300, archive_hours: 24, retry_hours: 72 }, manual_only: ["multipart_uploads"], scheduler_status: { started: true, passes: 1, last: null } } });
    if (path.endsWith("/deleted-papers")) return route.fulfill({ json: { total: 1, papers: [{ id: "paper-1", title: "Discarded timing paper", deleted_at: "2026-11-04T00:00:00Z" }] } });
    if (path.endsWith("/jobs") && route.request().method() === "POST") {
      const input = route.request().postDataJSON(); requests.push(input);
      return route.fulfill({ status: 202, json: { job_id: `cleanup-${requests.length}`, operation: input.operation, dry_run: input.dry_run, stage: "QUEUED", progress: 0, report_available: false, request: input } });
    }
    const id = Number(path.match(/cleanup-(\d+)/)?.[1] || requests.length);
    const input = requests[id - 1] || {};
    if (path.endsWith("/report")) return route.fulfill({ json: { job_id: `cleanup-${id}`, operation: "cleanup", dry_run: input.dry_run,
      expires_at: "2100-01-01T00:00:00Z", total: 1, ready: input.dry_run ? 1 : 0, protected: 0, completed: input.dry_run ? 0 : 1,
      skipped: 0, failed: 0, size_bytes: null, limited: false, errors: [], items: [{ id: "candidate-1", category: (input.categories as string[])[0], resource_key: "resource-1", size_bytes: null,
        state: input.dry_run ? "ready" : "completed", error: null, detail: {} }] } });
    return route.fulfill({ json: { job_id: `cleanup-${id}`, operation: "cleanup", dry_run: input.dry_run, stage: "COMPLETED", progress: 100, report_available: true,
      request: { operation: input.operation, categories: input.categories, paper_ids: input.paper_ids, indices: input.indices } } });
  });
  await page.goto("/system/cleanup");
  await expect(page.getByRole("heading", { name: "系统清理", exact: true })).toBeVisible();
  await expect(page.getByText("未启用", { exact: true })).toBeVisible();
  expect(requests).toHaveLength(0);
  await page.getByRole("tab", { name: "清理预览", exact: true }).click();
  await page.getByRole("button", { name: "预览清理", exact: true }).click();
  await expect(page.getByRole("button", { name: "执行本次预览", exact: true })).toBeEnabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "执行本次预览", exact: true })).toBeEnabled();
  expect(requests).toHaveLength(1);
  await page.getByRole("button", { name: "执行本次预览", exact: true }).click();
  expect(requests).toHaveLength(1);
  await page.getByRole("dialog").getByRole("button", { name: "确认执行", exact: true }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1].preview_job_id).toBe("cleanup-1");
  await expect(page.getByText(/执行：可清理 0，已清理 1/)).toBeVisible();
  await page.getByRole("tab", { name: "永久删除", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "Discarded timing paper" }).getByRole("checkbox").check();
  await page.getByRole("button", { name: "预览永久删除（1）", exact: true }).click();
  await expect(page.getByRole("button", { name: "执行本次预览", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "执行本次预览", exact: true }).click();
  const confirm = page.getByRole("dialog").getByRole("button", { name: "确认执行", exact: true });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("永久删除确认").fill("DELETE");
  await confirm.click();
  await expect.poll(() => requests.length).toBe(4);
  expect(requests[3].paper_ids).toEqual(["paper-1"]);
  await expect(page.getByText(/执行：可清理 0，已清理 1/)).toBeVisible();
});

for (const theme of ["light", "dark"]) {
  test(`cleanup page fits mobile and has readable contrast in ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.addInitScript(value => localStorage.setItem("paperbox:theme", value), theme);
    await page.route("**/api/admin/cleanup/policy", route => route.fulfill({ json: { automatic: false, interval_s: 3600, preview_hours: 24, batch_size: 100,
      days: { app_logs: 30, search_logs: 90, jobs: 90 }, log_rotation: { enabled: false, max_bytes: 52428800, backups: 5 }, upload_gc: { interval_s: 300, archive_hours: 24, retry_hours: 72 },
      manual_only: ["container_images_and_build_cache", "uv_npm_model_cache", "multipart_uploads", "database_vacuum_and_wal", "backups", "unregistered_artifacts"], scheduler_status: { started: true, passes: 0, last: null } } }));
    await page.goto("/system/cleanup");
    await expect(page.getByText("MinIO 未完成的分段上传", { exact: true })).toBeVisible();
    await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
    expect(await contrastFailures(page)).toEqual([]);
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).backgroundColor === getComputedStyle(document.body).backgroundColor)).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`cleanup-${theme}.png`), fullPage: true });
  });
}