import { expect, test } from "./fixtures";

test("task diagnostics show queue state, filter by paper, and preserve the error code", async ({ page }) => {
  const paperId = "11111111-2222-4333-8444-555555555555";
  const requests: URL[] = [];
  const job = { job_id: "failed-parse-job", paper_id: paperId, kind: "ingest", stage: "FAILED", progress: 45,
    error_code: "PARSE_FAILED", error_message: "parser offline",
    created_at: "2026-10-30T10:00:00Z", finished_at: "2026-10-30T10:01:05Z" };
  await page.route("**/api/jobs?*", (route) => {
    requests.push(new URL(route.request().url()));
    return route.fulfill({ json: { total: 1, jobs: [job] } });
  });
  await page.route("**/api/jobs/failed-parse-job", (route) => route.fulfill({ json: job }));
  await page.goto(`/manage/jobs?paper_id=${paperId}&stage=FAILED`);
  await expect(page.getByLabel("任务队列状态")).toContainText("并发上限 2");
  await page.getByRole("button", { name: "failed-parse", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "任务详情", exact: true });
  await expect(drawer.getByText("PARSE_FAILED", { exact: true })).toBeVisible();
  await expect(drawer.getByText("1 分 5 秒", { exact: true })).toBeVisible();
  await expect(drawer.getByRole("link", { name: "管理论文", exact: true })).toHaveAttribute("href", `/manage/library?paper=${paperId}`);
  await expect(drawer.getByRole("link", { name: "打开论文", exact: true })).toHaveCount(0);
  expect(requests.every((url) => url.searchParams.get("paper_id") === paperId && url.searchParams.get("stage") === "FAILED")).toBe(true);
});
