import { expect, test } from "./fixtures";

test("ZIP is submitted explicitly and reports rejected archive entries", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/papers/ingest/compressed", (route) => {
    calls++;
    expect(route.request().headers()["content-type"]).toContain("multipart/form-data");
    return route.fulfill({ status: 202, json: { accepted: 1, duplicate: 0, rejected: 0,
      entries_rejected: 1, results: [{ filename: "paper.pdf", status: "accepted", job_id: "zip-job" }] } });
  });
  await page.goto("/imports/new");
  await page.getByText("ZIP", { exact: true }).click();
  await page.locator("input[type=file]").setInputFiles({ name: "papers.zip", mimeType: "application/zip", buffer: Buffer.from("zip") });
  expect(calls).toBe(0);
  await page.getByRole("button", { name: "开始 ZIP 导入" }).click();
  await expect(page.getByText(/接收 1，重复 0，拒绝 1/)).toBeVisible();
  await expect(page.getByRole("link", { name: "查看任务", exact: true })).toHaveAttribute("href", "/manage/jobs?job=zip-job");
});

test("directory apply requires a current preview and confirmation", async ({ page }) => {
  const requests: { root: string; dry_run: boolean }[] = [];
  await page.route("**/api/papers/ingest/dir", (route) => {
    const input = route.request().postDataJSON(); requests.push(input);
    return route.fulfill({ json: { dry_run: input.dry_run, accepted: 1, duplicate: 0, rejected: 0,
      matched: 1, jobs: [{ filename: "a.pdf", status: "accepted", job_id: input.dry_run ? undefined : "dir-job" }] } });
  });
  await page.goto("/imports/new");
  await page.getByText("目录", { exact: true }).click();
  await page.getByLabel("服务端目录", { exact: true }).fill("/allowed/a");
  await expect(page.getByRole("button", { name: "正式导入目录" })).toBeDisabled();
  await page.getByRole("button", { name: "预览目录" }).click();
  await expect(page.getByRole("button", { name: "正式导入目录" })).toBeEnabled();
  await page.getByLabel("服务端目录", { exact: true }).fill("/allowed/b");
  await expect(page.getByRole("button", { name: "正式导入目录" })).toBeDisabled();
  await page.getByRole("button", { name: "预览目录" }).click();
  await expect(page.getByRole("button", { name: "正式导入目录" })).toBeEnabled();
  await page.getByRole("button", { name: "正式导入目录" }).click();
  expect(requests).toHaveLength(2);
  await page.getByRole("dialog").getByRole("button", { name: "确认导入" }).click();
  await expect(page.getByRole("link", { name: "查看任务", exact: true })).toBeVisible();
  expect(requests.map((item) => item.dry_run)).toEqual([true, true, false]);
  expect(requests[2].root).toBe("/allowed/b");
});