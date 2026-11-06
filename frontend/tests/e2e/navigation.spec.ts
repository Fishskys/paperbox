import { expandNavigation, expect, test } from "./fixtures";

test("legacy library links preserve filters, pagination and the reader return path", async ({ page }) => {
  await page.route("**/api/papers?*", (route) => route.fulfill({ json: { total: 100, papers: [
    { paper_id: "legacy-paper", title: "Legacy clock", authors: [], status: "INDEXED" },
  ] } }));
  await page.route("**/api/papers/legacy-paper", (route) => route.fulfill({ json: {
    paper_id: "legacy-paper", title: "Legacy clock", authors: [], status: "AWAITING_FILE", files: [],
  } }));
  await page.goto("/library?q=clock&author=Chen&status=INDEXED&page=3#results");
  await expect(page).toHaveURL(/\/search\/standard\?/);
  const source = new URL(page.url());
  expect(Object.fromEntries(source.searchParams)).toEqual({ q: "clock", author: "Chen", status: "INDEXED", page: "3", scope: "title", sort: "newest" });
  expect(source.hash).toBe("#results");
  const history = await page.evaluate(() => window.history.length);
  await page.getByRole("link", { name: "Legacy clock", exact: true }).click();
  await expect(page).toHaveURL(/reading\/papers\/legacy-paper/);
  await page.getByRole("button", { name: "返回来源页面" }).click();
  await expect(page).toHaveURL(source.toString());
  expect(await page.evaluate(() => window.history.length)).toBe(history + 1);
});

test("unfiltered conventional search browses the full library including metadata-only papers", async ({ page }) => {
  const requests: URL[] = [];
  await page.route("**/api/papers?*", (route) => {
    requests.push(new URL(route.request().url()));
    return route.fulfill({ json: { total: 1, papers: [{ paper_id: "shell", title: "Metadata only", authors: [], status: "AWAITING_FILE", files: [] }] } });
  });
  await page.goto("/search/standard");
  await expect(page.getByText("1 篇论文", { exact: true })).toBeVisible();
  expect(requests.at(-1)?.searchParams.has("status")).toBe(false);
  expect(requests.at(-1)?.searchParams.has("q")).toBe(false);
  await expect(page.locator('.sidebar a[href="/library"]')).toHaveCount(0);
  const reading = page.locator(".sidebar .nav-group").filter({ has: page.locator(".nav-group-title", { hasText: "阅读" }) });
  await expect(reading.locator(".nav-item")).toHaveText(["最近阅读"]);
  await page.getByRole("button", { name: "Metadata only", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "论文信息", exact: true }).getByText("待补充 PDF 原文")).toBeVisible();
  await expect(page.getByRole("button", { name: /^管理论文 / })).toHaveCount(0);
});

test("legacy task links resolve to paper management without losing job or filter state", async ({ page }) => {
  await page.route("**/api/jobs?*", (route) => route.fulfill({ json: { total: 0, jobs: [] } }));
  await page.route("**/api/jobs/job-1", (route) => route.fulfill({ json: { job_id: "job-1", stage: "FAILED", progress: 0 } }));
  await page.goto("/imports/jobs?paper_id=p-1&stage=FAILED&page=2&job=job-1#details");
  await expect(page).toHaveURL(/\/manage\/jobs\?/);
  expect(new URL(page.url()).search).toBe("?paper_id=p-1&stage=FAILED&page=2&job=job-1");
  expect(new URL(page.url()).hash).toBe("#details");
  await expect(page.getByRole("heading", { name: "处理任务", exact: true })).toBeVisible();
  await page.getByRole("dialog", { name: "任务详情", exact: true }).getByRole("button", { name: "关闭", exact: true }).click();
  const management = page.locator(".sidebar .nav-group").filter({ has: page.locator(".nav-group-title", { hasText: "论文管理" }) });
  await expandNavigation(page, "论文管理");
  await expect(management.getByRole("link", { name: "处理任务", exact: true })).toBeVisible();
  const imports = page.locator(".sidebar .nav-group").filter({ has: page.locator(".nav-group-title", { hasText: /^导入$/ }) });
  await expect(imports.getByRole("link", { name: "处理任务", exact: true })).toHaveCount(0);
});
