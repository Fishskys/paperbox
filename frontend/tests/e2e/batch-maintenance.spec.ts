import { expect, identity, test } from "./fixtures";
import { maintenanceFixture } from "./maintenance-fixture";

for (const width of [1440, 390]) {
  test(`bounded management previews scope and exposes per-paper jobs at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const papers = [{ paper_id: "one", title: "First paper", authors: [], status: "INDEXED", files: [{ mime_type: "application/pdf" }] },
      { paper_id: "two", title: "Second paper", authors: [], status: "AWAITING_FILE", files: [] }];
    await page.route("**/api/papers?*", route => route.fulfill({ json: { total: 2, papers } }));
    const requests = await maintenanceFixture(page, input => ({ dry_run: input.dry_run, papers: 2, failed: 0, queued: input.dry_run ? 0 : 1, skipped_no_file: 1, skipped_active: 0, changed_papers: 1,
      changes: [{ paper_id: "one", field: "year", old: 2026, new: 2015, action: "update" }],
      items: [{ paper_id: "one", title: "First paper", status: input.dry_run ? "ready" : "queued", ...(!input.dry_run ? { job_id: "child-job" } : {}) },
        { paper_id: "two", title: "Second paper", status: "skipped_no_file" }] }));
    await page.goto("/manage/library");
    await page.getByRole("checkbox", { name: "选择论文 First paper", exact: true }).check();
    await page.getByRole("checkbox", { name: "选择论文 Second paper", exact: true }).check();
    await expect(page.getByText("已选 2 / 100 篇", { exact: true })).toBeVisible();
    await page.getByRole("combobox", { name: "批量操作", exact: true }).click();
    await page.locator(".ant-select-item-option-content").getByText("重建索引", { exact: true }).click();
    await page.getByRole("button", { name: "预览所选论文", exact: true }).click();
    await expect(page.getByText("无原文，已跳过", { exact: true })).toBeVisible();
    const execute = page.getByRole("button", { name: "执行所选维护", exact: true });
    await expect(execute).toBeEnabled();
    expect(requests[0].paper_ids).toEqual(["one", "two"]);
    await execute.click();
    expect(requests).toHaveLength(1);
    await page.getByRole("dialog", { name: "确认批量维护", exact: true }).getByRole("button", { name: "确认执行", exact: true }).click();
    await expect(page.getByRole("link", { name: "查看处理任务", exact: true })).toHaveAttribute("href", "/manage/jobs?job=child-job");
    expect(requests).toHaveLength(2);
    expect(requests[1].preview_job_id).toBe("task-1");
    await expect(page.locator(".ant-modal-mask")).toBeHidden();
    await page.screenshot({ path: testInfo.outputPath(`batch-${width}.png`), fullPage: true, animations: "disabled" });
    await page.reload();
    await expect(page.getByRole("link", { name: "查看处理任务", exact: true })).toBeVisible();
    expect(requests).toHaveLength(2);
    await expect(execute).toBeDisabled();
  });
}

test("write keys keep single-paper management without administrator batch controls", async ({ page }) => {
  await page.route("**/api/auth/context", route => route.fulfill({ json: identity("write") }));
  await page.route("**/api/papers?*", route => route.fulfill({ json: { total: 1, papers: [{ paper_id: "one", title: "First paper", authors: [], status: "AWAITING_FILE", files: [] }] } }));
  await page.goto("/manage/library");
  await expect(page.getByRole("button", { name: "管理论文 First paper", exact: true })).toBeVisible();
  await expect(page.getByLabel("批量维护", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
});
