import { expect, test } from "./fixtures";
import { maintenanceFixture } from "./maintenance-fixture";

for (const kind of ["index", "metadata"] as const) {
  test(`${kind} maintenance previews, invalidates changes and confirms writes`, async ({ page }) => {
    const metadataReport = (input: Record<string, unknown>) => ({ dry_run: input.dry_run, papers: 1, changed_papers: 1, skipped_no_file: 0, failed: 0, changes: [{ paper_id: "paper-1", field: "title", old: "old", new: "new", action: "update" }], errors: [], note: "" });
    const requests: Record<string, unknown>[] = kind === "metadata" ? await maintenanceFixture(page, metadataReport) : [];
    if (kind === "index") await page.route("**/api/papers/reindex", (route) => {
      const input = route.request().postDataJSON(); requests.push(input);
      return route.fulfill({ json: kind === "index"
        ? { dry_run: input.dry_run, selected: 1, queued: input.dry_run ? 0 : 1, job_ids: input.dry_run ? [] : ["maintenance-job"], skipped: [], reasons: [{ code: "missing_chunks", detail: "缺少片段", papers: 1 }], skipped_reasons: [], note: "" }
        : { dry_run: input.dry_run, papers: 1, changed_papers: 1, skipped_no_file: 0, failed: 0, changes: [{ paper_id: "paper-1", field: "title", old: "old", new: "new", action: "update" }], errors: [], note: "" } });
    });
    await page.goto(`/system/${kind}`);
    await expect(page.getByRole("heading", { name: kind === "index" ? "索引维护" : "元数据维护", exact: true })).toBeVisible({ timeout: 15000 });
    expect(requests).toHaveLength(0);
    await expect(page.getByRole("button", { name: "执行维护" })).toBeDisabled();
    await page.getByRole("button", { name: "预览维护" }).click();
    await expect(page.getByRole("button", { name: "执行维护" })).toBeEnabled();
    await page.getByLabel("维护论文 ID").fill("paper-1");
    await expect(page.getByRole("button", { name: "执行维护" })).toBeDisabled();
    await page.getByRole("button", { name: "预览维护" }).click();
    await expect(page.getByRole("button", { name: "执行维护" })).toBeEnabled();
    await page.getByRole("button", { name: "执行维护" }).click();
    expect(requests).toHaveLength(2);
    await page.getByRole("dialog").getByRole("button", { name: "确认执行" }).click();
    await expect.poll(() => requests.length).toBe(3);
    expect(requests.map((value) => value.dry_run)).toEqual([true, true, false]);
    if (kind === "index") await expect(page.getByRole("link", { name: "任务 maintenance-job" })).toBeVisible();
    else await expect(page.getByText(/已完成：处理 1/)).toBeVisible();
  });
}

test("identifier checks show incomplete and drift details without repairs", async ({ page }) => {
  await page.route("**/api/consistency/doi?*", (route) => route.fulfill({ json: { consistent: false, errors: ["partial check"], truncated: true,
    drifts: [{ paper_id: "paper-1", scheme: "doi", kind: "mismatch", mirror_value: "10/old", identifier_values: ["10/new"], detail: "different" }] } }));
  await page.goto("/system/data");
  await page.getByRole("button", { name: "检查标识" }).click();
  await expect(page.getByText("结果不完整", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "paper-1" })).toHaveAttribute("href", "/manage/library?paper=paper-1");
  await expect(page.getByText("10/new", { exact: true })).toBeVisible();
});

test("full data checks may finish beyond the default request timeout", async ({ page }) => {
  let running = true;
  const requests = await maintenanceFixture(page, () => {
    return { consistent: true, checked_at: "2026-10-30T09:00:00Z", errors: [], problems: [], truncated: false,
      totals: { papers_live: 1, documents_os: 1, objects_minio: 1, problems: 0 } };
  }, () => running);
  await page.goto("/system/data");
  await page.clock.install();
  await page.getByRole("button", { name: "开始检查" }).click();
  await expect(page.getByText(/执行中/)).toBeVisible();
  await page.clock.fastForward(45000);
  running = false;
  await page.clock.resume();
  await expect(page.getByText("数据一致", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("数据一致", { exact: true })).toBeVisible();
  expect(requests).toHaveLength(1);
});

test("an ambiguous submission is recovered by request number without repeating the write", async ({ page }) => {
  let writes = 0;
  await page.addInitScript(() => Object.defineProperty(Crypto.prototype, "randomUUID", { value: undefined, configurable: true }));
  await page.route("**/api/maintenance/jobs**", async route => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON().request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      writes++; await route.abort("failed"); return;
    }
    if (route.request().url().endsWith("/report")) return route.fulfill({ json: { consistent: true, errors: [], problems: [], totals: {} } });
    return route.fulfill({ json: { job_id: "accepted-without-response", operation: "consistency", dry_run: true, stage: "COMPLETED", progress: 100, report_available: true } });
  });
  await page.goto("/system/data");
  await page.getByRole("button", { name: "开始检查", exact: true }).click();
  await expect(page.getByText(/提交结果不明/)).toBeVisible();
  await page.reload();
  await expect(page.getByText("数据一致", { exact: true })).toBeVisible();
  expect(writes).toBe(1);
});

test("an unavailable saved task can be cleared without starting a replacement", async ({ page }) => {
  let writes = 0;
  await page.route("**/api/maintenance/jobs**", route => {
    if (route.request().method() === "POST") writes++;
    return route.fulfill({ status: 404, json: { detail: "maintenance job not found" } });
  });
  await page.goto("/system/data?task=removed-task");
  await expect(page.getByRole("button", { name: /开始检查/ })).toBeDisabled();
  await page.getByRole("button", { name: "清除失效任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "开始检查", exact: true })).toBeEnabled();
  expect(new URL(page.url()).searchParams.has("task")).toBe(false);
  expect(writes).toBe(0);
});
