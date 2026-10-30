import { expect, test } from "./fixtures";

test("conventional search uses metadata scopes, server pagination and restorable history", async ({ page }) => {
  const requests: URL[] = [];
  await page.route("**/api/search", () => { throw new Error("conventional search must not enter model search"); });
  await page.route("**/api/papers?*", (route) => {
    const url = new URL(route.request().url()); requests.push(url);
    return route.fulfill({ json: {
      total: 80, papers: [{ paper_id: "catalog-one", title: "Timing catalog paper", authors: ["Alice Chen"], status: "INDEXED" }],
      facets: { authors: [{ key: "Alice Chen", count: 80 }], venue: [{ key: "JSSC", count: 80 }] },
    } });
  });
  await page.goto("/search/standard");
  await page.getByLabel("检索内容", { exact: true }).fill("clock");
  await page.getByLabel("检索内容", { exact: true }).press("Enter");
  await expect(page).toHaveURL(/q=clock/);
  await page.getByRole("combobox", { name: "检索字段" }).click();
  await page.locator(".ant-select-item-option-content").getByText("作者", { exact: true }).click();
  await expect(page).toHaveURL(/scope=authors/);
  const jumps = page.locator(".ant-pagination-options-quick-jumper input");
  await jumps.first().fill("2"); await jumps.first().press("Enter");
  await expect.poll(() => requests.at(-1)?.searchParams.get("offset")).toBe("20");
  await page.reload();
  await expect(page).toHaveURL(/page=2/);
  expect(requests.at(-1)?.searchParams.get("scope")).toBe("authors");
  await page.goto("/search/history");
  await expect(page.getByRole("link", { name: "clock" }).first()).toHaveAttribute("href", /\/search\/standard\?/);
});