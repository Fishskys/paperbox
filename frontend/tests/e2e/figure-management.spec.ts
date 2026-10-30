import { expect, test } from "./fixtures";

const paper = { paper_id: "fig-paper", title: "Figure paper", authors: [], status: "INDEXED", files: [{ mime_type: "application/pdf" }] };
function figure(id: string) {
  return { figure_id: id, display_name: `Figure ${id}`, caption_text: `Caption ${id}`, match_status: "needs_review", native_format: "png",
    native_url: `/api/papers/fig-paper/figures/${id}/file?variant=native`, parts: [{ part: 1, page: 2 }] };
}

test("figure management extracts, refreshes jobs, downloads a pinned ZIP and previews deletions", async ({ page }) => {
  let figures: ReturnType<typeof figure>[] = [];
  let generation: string | null = null;
  let active = false;
  let finish = false;
  const extractions: unknown[] = [];
  const deletions: string[] = [];
  let archive = "";
  await page.route("**/api/papers?*", r => r.fulfill({ json: { total: 1, papers: [paper] } }));
  await page.route("**/api/papers/fig-paper", r => r.fulfill({ json: paper }));
  await page.route("**/api/papers/fig-paper/metadata", r => r.fulfill({ json: { sources: [], provenance: {} } }));
  await page.route("**/api/papers/fig-paper/figures**", r => {
    const req = r.request(); const url = new URL(req.url());
    if (url.pathname.endsWith("/extract")) {
      extractions.push(req.postDataJSON()); active = true; finish = false;
      return r.fulfill({ status: 202, json: { job_id: "figure-job", status: "queued", generation_id: "new-generation" } });
    }
    if (url.pathname.endsWith("/download")) {
      archive = url.search;
      return r.fulfill({ contentType: "application/zip", body: Buffer.from("PK-test-archive") });
    }
    if (req.method() === "DELETE") {
      deletions.push(url.pathname + url.search);
      const single = url.pathname.endsWith("/1"); const count = single ? 1 : figures.length;
      if (url.searchParams.get("dry_run") === "false") {
        figures = single ? figures.filter(f => f.figure_id !== "1") : [];
        if (!single) generation = null;
      }
      return r.fulfill({ json: { figures_deleted: count, objects_deleted: 0, cleanup_pending: false, dry_run: url.searchParams.get("dry_run") === "true" } });
    }
    if (active && finish) { active = false; generation = "new-generation"; figures = [figure("1"), figure("2")]; }
    return r.fulfill({ json: { generation_id: generation, status: generation ? "partial" : active ? "queued" : "not_extracted",
      job_id: active ? "figure-job" : null, total: figures.length, figures,
      warnings: generation ? [{ code: "AUTOMATIC_CANDIDATES", message: "请核对自动提取结果" }] : [],
    } });
  });
  await page.goto("/manage/library?paper=fig-paper");
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByRole("tab", { name: "附图", exact: true }).click();
  await expect(drawer.getByRole("button", { name: "下载全部附图", exact: true })).toBeDisabled();
  await drawer.getByRole("button", { name: "提取附图", exact: true }).click();
  expect(extractions).toEqual([{ force: false }]);
  await expect(drawer.getByRole("link", { name: "查看附图任务" })).toHaveAttribute("href", "/manage/jobs?job=figure-job");
  await expect(drawer.getByRole("button", { name: "提取附图", exact: true })).toBeDisabled();
  finish = true;
  await expect(drawer.getByText("Caption 1", { exact: true })).toBeVisible();
  await expect(drawer.getByText("待复核", { exact: true })).toHaveCount(2);
  await expect(drawer.getByText("请核对自动提取结果", { exact: true })).toBeVisible();
  await drawer.getByRole("button", { name: "重新提取附图", exact: true }).click();
  const confirm = page.getByRole("dialog", { name: "重新提取附图", exact: true });
  expect(extractions).toHaveLength(1);
  await confirm.getByRole("button", { name: "确认执行" }).click();
  await expect.poll(() => extractions).toEqual([{ force: false }, { force: true }]);
  await expect(drawer.getByText("Caption 1", { exact: true })).toBeVisible();
  finish = true;
  await expect(drawer.getByRole("button", { name: "重新提取附图", exact: true })).toBeEnabled();
  const zip = page.waitForEvent("download");
  await drawer.getByRole("button", { name: "下载全部附图", exact: true }).click();
  expect((await zip).suggestedFilename()).toBe("fig-paper-figures.zip");
  expect(archive).toBe("?generation_id=new-generation&variant=all");
  await drawer.getByRole("button", { name: "删除附图 Figure 1", exact: true }).click();
  const remove = page.getByRole("dialog", { name: "删除附图", exact: true });
  expect(deletions).toEqual(["/api/papers/fig-paper/figures/1?dry_run=true"]);
  await remove.getByRole("button", { name: /^取\s*消$/ }).click();
  expect(deletions).toHaveLength(1);
  await drawer.getByRole("button", { name: "删除附图 Figure 1", exact: true }).click();
  await remove.getByRole("button", { name: "确认执行" }).click();
  await expect(drawer.getByText("Caption 1", { exact: true })).toHaveCount(0);
  await expect(drawer.getByText("Caption 2", { exact: true })).toBeVisible();
  await drawer.getByRole("button", { name: "删除全部附图", exact: true }).click();
  await remove.getByRole("button", { name: "确认执行" }).click();
  await expect(drawer.getByText("暂无已生成附图", { exact: true })).toBeVisible();
  expect(deletions.slice(-2)).toEqual(["/api/papers/fig-paper/figures?dry_run=true", "/api/papers/fig-paper/figures?dry_run=false"]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(async () => { const box = await drawer.boundingBox(); return !!box && box.x >= 0 && box.width <= 390; }).toBe(true);
});

test("figure polling follows active jobs and stops when the figure tab is closed", async ({ page }) => {
  let reads = 0;
  await page.clock.install();
  await page.route("**/api/papers?*", r => r.fulfill({ json: { total: 1, papers: [paper] } }));
  await page.route("**/api/papers/fig-paper", r => r.fulfill({ json: paper }));
  await page.route("**/api/papers/fig-paper/metadata", r => r.fulfill({ json: { sources: [], provenance: {} } }));
  await page.route("**/api/papers/fig-paper/figures?*", r => {
    reads++;
    return r.fulfill({ json: { generation_id: "old-generation", status: "completed", job_id: "running-job", total: 1, figures: [figure("1")] } });
  });
  await page.goto("/manage/library?paper=fig-paper");
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByRole("tab", { name: "附图", exact: true }).click();
  await expect(drawer.getByText("Caption 1", { exact: true })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "重新提取附图", exact: true })).toBeDisabled();
  await page.clock.fastForward(2100);
  await expect.poll(() => reads).toBe(2);
  await drawer.getByRole("tab", { name: "元数据", exact: true }).click();
  await expect(drawer.getByRole("button", { name: "重新提取附图", exact: true })).toHaveCount(0);
  const stopped = reads;
  await page.clock.fastForward(10000);
  expect(reads).toBe(stopped);
});

test("figure failures preserve published results and retries require an explicit action", async ({ page }) => {
  let reads = 0; let extracts = 0; let failRead = true;
  await page.route("**/api/papers?*", r => r.fulfill({ json: { total: 1, papers: [paper] } }));
  await page.route("**/api/papers/fig-paper", r => r.fulfill({ json: paper }));
  await page.route("**/api/papers/fig-paper/metadata", r => r.fulfill({ json: { sources: [], provenance: {} } }));
  await page.route("**/api/papers/fig-paper/figures**", r => {
    if (r.request().method() === "POST") { extracts++; return r.fulfill({ status: 503, json: { detail: { message: "FIGURE_QUEUE_UNAVAILABLE" } } }); }
    reads++;
    return failRead ? r.fulfill({ status: 503, json: { detail: "figure list unavailable" } }) : r.fulfill({ json: {
      generation_id: "old-generation", status: "completed", job_id: null, total: 1, figures: [figure("1")], last_error: "new extraction failed",
    } });
  });
  await page.goto("/manage/library?paper=fig-paper");
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByRole("tab", { name: "附图", exact: true }).click();
  await expect(drawer.getByText("figure list unavailable", { exact: true })).toBeVisible();
  expect(reads).toBe(1);
  failRead = false;
  await drawer.getByRole("button", { name: "刷新附图" }).click();
  await expect(drawer.getByText("Caption 1", { exact: true })).toBeVisible();
  await expect(drawer.getByText("new extraction failed", { exact: true })).toBeVisible();
  await drawer.getByRole("button", { name: "重新提取附图", exact: true }).click();
  await page.getByRole("dialog", { name: "重新提取附图", exact: true }).getByRole("button", { name: "确认执行" }).click();
  await expect(drawer.getByText(/FIGURE_QUEUE_UNAVAILABLE/)).toBeVisible();
  expect(extracts).toBe(1);
  await expect(drawer.getByText("Caption 1", { exact: true })).toBeVisible();
});

test("metadata-only papers cannot extract figures or trigger archive auto-extraction", async ({ page }) => {
  const shell = { ...paper, status: "AWAITING_FILE", files: [] };
  const calls: string[] = [];
  await page.route("**/api/papers?*", r => r.fulfill({ json: { total: 1, papers: [shell] } }));
  await page.route("**/api/papers/fig-paper", r => r.fulfill({ json: shell }));
  await page.route("**/api/papers/fig-paper/metadata", r => r.fulfill({ json: { sources: [], provenance: {} } }));
  await page.route("**/api/papers/fig-paper/figures**", r => {
    calls.push(r.request().method() + " " + new URL(r.request().url()).pathname);
    return r.fulfill({ json: { generation_id: null, status: "not_extracted", job_id: null, total: 0, figures: [] } });
  });
  await page.goto("/manage/library?paper=fig-paper");
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByRole("tab", { name: "附图", exact: true }).click();
  await expect(drawer.getByRole("button", { name: "提取附图", exact: true })).toBeDisabled();
  await expect(drawer.getByRole("button", { name: "下载全部附图", exact: true })).toBeDisabled();
  await expect(drawer.getByText("补传 PDF 原文后可提取附图", { exact: true })).toBeVisible();
  expect(calls).toEqual(["GET /api/papers/fig-paper/figures"]);
});
