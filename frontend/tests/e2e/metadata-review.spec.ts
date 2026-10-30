import { expect, test } from "./fixtures";

test("review pages reach sources and conflicts beyond 200 records independently", async ({ page }) => {
  const requests: URL[] = [];
  await page.route("**/api/metadata/review?*", (route) => {
    const url = new URL(route.request().url()); requests.push(url);
    return route.fulfill({ json: { total: 205, conflicts_total: 205,
      items: [{ source_id: `s-${url.searchParams.get("offset")}`, source_ref: `source-${url.searchParams.get("offset")}`, source_type: "import_file", match_status: "pending" }],
      conflicts: [{ paper_id: "p", field: `field-${url.searchParams.get("conflicts_offset")}`, kept: "a", rejected: "b" }],
    } });
  });
  await page.goto("/imports/metadata?tab=review");
  const jumps = page.locator(".ant-pagination-options-quick-jumper input");
  await jumps.first().fill("11"); await jumps.first().press("Enter");
  await expect(page.getByText("source-200", { exact: true })).toBeVisible();
  await jumps.last().fill("11"); await jumps.last().press("Enter");
  await expect(page.getByText("field-200", { exact: true })).toBeVisible();
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("200");
  expect(requests.at(-1)?.searchParams.get("conflicts_offset")).toBe("200");
});

test("metadata review is inside import and associates sources before resolving conflicts", async ({
  page,
}) => {
  let items = [
    {
      source_id: "source-1",
      source_ref: "ieee:one",
      source_type: "import_file",
      match_status: "ambiguous",
      match_method: "title",
    },
  ];
  let conflicts = [
    {
      paper_id: "paper-1",
      field: "year",
      kept: 2024,
      rejected: 2025,
      provenance_id: "claim-year",
    },
    {
      paper_id: "paper-1",
      field: "venue",
      kept: "Old venue",
      rejected: "New venue",
      provenance_id: "claim-venue",
    },
  ];
  let reads = 0;
  const writes: { path: string; body: unknown }[] = [];
  await page.route("**/api/metadata/review?*", (route) => {
    reads++;
    return route.fulfill({ json: { total: items.length, items, conflicts } });
  });
  await page.route("**/api/papers?*", (route) =>
    route.fulfill({
      json: {
        total: 1,
        papers: [
          {
            paper_id: "paper-1",
            title: "Target paper",
            authors: [],
            year: 2024,
          },
        ],
      },
    }),
  );
  await page.route("**/api/metadata/sources/source-1/attach", (route) => {
    writes.push({ path: "attach", body: route.request().postDataJSON() });
    items = [];
    return route.fulfill({
      json: {
        source_id: "source-1",
        paper_id: "paper-1",
        merged_fields: ["abstract"],
      },
    });
  });
  await page.route("**/api/papers/paper-1/metadata/**", (route) => {
    const body = route.request().postDataJSON();
    writes.push({ path: new URL(route.request().url()).pathname, body });
    conflicts = conflicts.filter(
      (conflict) => conflict.provenance_id !== body.provenance_id,
    );
    return route.fulfill({ json: { paper_id: "paper-1", field: body.field } });
  });
  await page.goto("/imports/metadata");
  await expect(
    page.getByRole("heading", { name: "元数据导入", exact: true }),
  ).toBeVisible();
  expect(reads).toBe(0);
  await page.getByRole("tab", { name: "元数据复核", exact: true }).click();
  await expect(page).toHaveURL(/tab=review/);
  await expect(page.getByText("ieee:one", { exact: true })).toBeVisible();
  await expect(
    page
      .locator(".sidebar")
      .getByRole("link", { name: "元数据复核", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "关联论文", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "关联论文", exact: true });
  await drawer.getByRole("combobox", { name: "目标论文", exact: true }).click();
  await page.getByText("Target paper · 2024", { exact: true }).click();
  await drawer.getByRole("button", { name: "确认关联", exact: true }).click();
  await expect(page.getByText("ieee:one", { exact: true })).toHaveCount(0);
  expect(writes[0]).toEqual({ path: "attach", body: { paper_id: "paper-1" } });
  const year = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "year", exact: true }) });
  await year.getByRole("button", { name: "保留现值", exact: true }).click();
  await page
    .getByRole("dialog", { name: "保留现值", exact: true })
    .getByRole("button", { name: "确认裁决", exact: true })
    .click();
  await expect(year).toHaveCount(0);
  expect(writes[1]).toEqual({
    path: "/api/papers/paper-1/metadata/conflicts/dismiss",
    body: { field: "year", provenance_id: "claim-year" },
  });
  await page.getByRole("button", { name: "采纳被拒值", exact: true }).click();
  await expect(
    page
      .getByRole("dialog", { name: "采纳被拒值", exact: true })
      .getByText('"New venue"', { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("dialog", { name: "采纳被拒值", exact: true })
    .getByRole("button", { name: "确认裁决", exact: true })
    .click();
  await expect(page.getByText("暂无待复核冲突", { exact: true })).toBeVisible();
  expect(writes[2]).toEqual({
    path: "/api/papers/paper-1/metadata/rollback",
    body: { field: "venue", provenance_id: "claim-venue" },
  });
});

test("review failures remain visible and missing provenance cannot be adjudicated", async ({
  page,
}) => {
  let failed = true;
  await page.route("**/api/metadata/review?*", (route) =>
    route.fulfill(
      failed
        ? { status: 503, json: { detail: "review unavailable" } }
        : {
            json: {
              total: 0,
              items: [],
              conflicts: [
                {
                  paper_id: "paper-1",
                  field: "year",
                  kept: 2024,
                  rejected: 2025,
                },
              ],
            },
          },
    ),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/imports/metadata?tab=review");
  await expect(
    page.getByText("review unavailable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("暂无待复核冲突", { exact: true })).toHaveCount(
    0,
  );
  failed = false;
  await page.getByRole("button", { name: "刷新复核列表", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "保留现值", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "采纳被拒值", exact: true }),
  ).toBeDisabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
