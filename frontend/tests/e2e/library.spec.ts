import { expect, test } from "./fixtures";

test("both library pagers jump to the same filtered page and survive refresh", async ({
  page,
}) => {
  const requests: URL[] = [];
  await page.route("**/api/papers?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const offset = Number(url.searchParams.get("offset"));
    return route.fulfill({
      json: {
        total: 200,
        limit: 20,
        offset,
        papers: [
          {
            paper_id: `paper-${offset}`,
            title: `Library paper ${offset}`,
            authors: ["Chen"],
            status: "INDEXED",
          },
        ],
      },
    });
  });
  await page.goto("/library?q=clock&author=Chen&status=INDEXED");
  const pagers = page.locator(".ant-table-pagination");
  await expect(pagers).toHaveCount(2);
  const jumps = page.locator(".ant-pagination-options-quick-jumper input");
  await expect(jumps).toHaveCount(2);
  await jumps.first().fill("3");
  await jumps.first().press("Enter");
  await expect(page).toHaveURL(/page=3/);
  await expect(
    page.getByRole("link", { name: "Library paper 40" }),
  ).toBeVisible();
  await expect(pagers.locator(".ant-pagination-item-active")).toHaveText([
    "3",
    "3",
  ]);
  await jumps.last().fill("2");
  await jumps.last().press("Enter");
  await expect(page).toHaveURL(/page=2/);
  await expect(
    page.getByRole("link", { name: "Library paper 20" }),
  ).toBeVisible();
  await page.reload();
  await expect(pagers.locator(".ant-pagination-item-active")).toHaveText([
    "2",
    "2",
  ]);
  expect(
    requests.every(
      (url) =>
        url.searchParams.get("q") === "clock" &&
        url.searchParams.get("author") === "Chen" &&
        url.searchParams.get("status") === "INDEXED",
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(jumps.first()).toBeVisible();
  await expect(jumps.last()).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
