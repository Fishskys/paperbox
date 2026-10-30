import { expect, test } from "./fixtures";

for (const kind of ["index", "metadata"] as const) {
  test(`${kind} maintenance previews, invalidates changes and confirms writes`, async ({ page }) => {
    const requests: { dry_run: boolean }[] = [];
    await page.route(kind === "index" ? "**/api/papers/reindex" : "**/api/metadata/reextract", (route) => {
      const input = route.request().postDataJSON(); requests.push(input);
      return route.fulfill({ json: kind === "index"
        ? { dry_run: input.dry_run, selected: 1, queued: input.dry_run ? 0 : 1, job_ids: input.dry_run ? [] : ["maintenance-job"], skipped: [], reasons: [{ code: "missing_chunks", detail: "缺少片段", papers: 1 }], skipped_reasons: [], note: "" }
        : { dry_run: input.dry_run, papers: 1, changed_papers: 1, skipped_no_file: 0, failed: 0, changes: [{ paper_id: "paper-1", field: "title", old: "old", new: "new", action: "update" }], errors: [], note: "" } });
    });
    await page.goto(`/system/${kind}`);
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