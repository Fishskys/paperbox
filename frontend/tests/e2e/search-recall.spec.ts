import { expect, test } from "./fixtures";

test("smart search preserves a paper recall pool across pages and reports rerank fallback", async ({ page }) => {
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/search", (route) => {
    const body = route.request().postDataJSON(); requests.push(body);
    return route.fulfill({ json: { total: 200, took_ms: 20, mode: "keyword", backend: "native", recalled_papers: 35,
      rerank: { enabled: false, status: "disabled" }, rewrite: { applied: false },
      results: [{ paper_id: `p-${body.offset}`, title: `Result ${body.offset}`, authors: [], evidence: [] }],
    } });
  });
  await page.goto("/search?q=timing&mode=keyword&recall_k=35&top_k=20&rerank=true");
  await expect(page.getByText("重排：服务未启用", { exact: true })).toBeVisible();
  await page.locator(".ant-pagination-item-2").click();
  await expect(page.getByRole("link", { name: "Result 20", exact: true })).toBeVisible();
  expect(requests.at(-1)).toMatchObject({ recall_k: 35, top_k: 20, offset: 20, rewrite: false, backend: "native" });
  await page.reload();
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByRole("link", { name: "Result 20", exact: true })).toBeVisible();
});

test("reference exclusion defaults on and restores across pagination and reload", async ({ page }) => {
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/search", (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    return route.fulfill({ json: { total: 35, took_ms: 20, mode: "hybrid", backend: "native", recalled_papers: 35,
      results: [{ paper_id: `p-${body.offset}`, title: `Result ${body.offset}`, authors: [], evidence: [] }],
    } });
  });
  await page.goto("/search?q=timing&recall_k=35&top_k=20");
  const exclude = page.getByRole("switch", { name: "排除参考文献" });
  await expect(exclude).toBeChecked();
  await expect(page.getByRole("link", { name: "Result 0", exact: true })).toBeVisible();
  expect(requests.at(-1)?.filters).not.toHaveProperty("include_references");
  await exclude.click();
  await page.getByRole("button", { name: "搜索论文" }).click();
  await expect.poll(() => requests.at(-1)?.filters).toHaveProperty("include_references", true);
  await page.locator(".ant-pagination-item-2").click();
  await expect(page.getByRole("link", { name: "Result 20", exact: true })).toBeVisible();
  await page.reload();
  await expect(exclude).not.toBeChecked();
  await expect(page.getByRole("link", { name: "Result 20", exact: true })).toBeVisible();
  expect(requests.at(-1)?.filters).toHaveProperty("include_references", true);
  await exclude.click();
  await page.getByRole("button", { name: "搜索论文" }).click();
  await expect(page).not.toHaveURL(/include_references=/);
  await expect.poll(() => requests.at(-1)?.filters).not.toHaveProperty("include_references");
  await expect(page.getByRole("link", { name: "Result 0", exact: true })).toBeVisible();
});
