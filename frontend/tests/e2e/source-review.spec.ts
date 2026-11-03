import { expect, test } from "./fixtures";

const source = { source_id: "source-a", source_ref: "doi:10.1234/imported", source_type: "import_file", match_status: "ambiguous" };
const match = { paper_id: null as string | null, match_status: "ambiguous", reason: "Publication DOI differs", conflicts: ["Different DOI"],
  candidates: [{ paper_id: "paper-a", title: "Conference publication", year: 2019, doi: "10.1234/conference", authors: ["Alice Smith"], filename: "conference.pdf" }] };
const initial = { source, revision: "a".repeat(64), values: { title: "Imported publication", authors: ["A. Smith"], year: 2019, "identifier:doi": "10.1234/imported" }, match, history: [] as object[] };

test("source details preview and confirm the unique match without duplicate writes", async ({ page }) => {
  let detail = structuredClone(initial);
  let done = false;
  const writes: object[] = [];
  await page.route("**/api/papers?*", (route) => route.fulfill({ json: { papers: [], total: 0 } }));
  await page.route("**/api/metadata/review?*", (route) => route.fulfill({ json: { items: done ? [] : [source], total: done ? 0 : 1, conflicts: [] } }));
  await page.route("**/api/metadata/sources/source-a", (route) => route.fulfill({ json: detail }));
  await page.route("**/api/metadata/sources/source-a/rematch", async (route) => {
    const body = route.request().postDataJSON(); writes.push(body);
    if (!body.apply) {
      detail = { ...detail, match: { ...match, match_status: "matched", paper_id: "paper-a", reason: "unique complete DOI filename" } };
      return route.fulfill({ json: detail });
    }
    done = true;
    return route.fulfill({ json: { ...detail, applied: true, source: { ...source, match_status: "matched", paper_id: "paper-a" } } });
  });
  await page.goto("/imports/metadata?tab=review");
  await page.getByRole("button", { name: "查看与处理" }).click();
  const drawer = page.getByRole("dialog", { name: "来源详情", exact: true });
  await expect(drawer.getByText("Imported publication", { exact: true })).toBeVisible();
  await expect(drawer.getByText("10.1234/conference", { exact: true })).toBeVisible();
  await drawer.getByRole("button", { name: "重新匹配", exact: true }).click();
  await expect(drawer.getByText("找到唯一匹配论文", { exact: true })).toBeVisible();
  expect(writes).toEqual([{ apply: false }]);
  await drawer.getByRole("button", { name: "应用唯一匹配", exact: true }).click();
  await page.getByRole("dialog", { name: "重新匹配", exact: true }).getByRole("button", { name: "确认处理" }).click();
  await expect(drawer).not.toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toMatchObject({ apply: true, expected_paper_id: "paper-a", revision: "a".repeat(64) });
});

test("reject needs no reason, preserves failures, and can be restored", async ({ page }) => {
  let detail = structuredClone(initial);
  let first = true;
  const actions: string[] = [];
  await page.route("**/api/papers?*", (route) => route.fulfill({ json: { papers: [], total: 0 } }));
  await page.route("**/api/metadata/review?*", (route) => {
    const rejected = new URL(route.request().url()).searchParams.get("status") === "rejected";
    const visible = rejected === (detail.source.match_status === "rejected");
    return route.fulfill({ json: { total: visible ? 1 : 0, items: visible ? [detail.source] : [], conflicts: [] } });
  });
  await page.route("**/api/metadata/sources/source-a", (route) => route.fulfill({ json: detail }));
  await page.route("**/api/metadata/sources/source-a/reject", (route) => {
    if (first) { first = false; return route.fulfill({ status: 503, json: { detail: "review save unavailable" } }); }
    actions.push("reject");
    detail = { ...detail, revision: "b".repeat(64), source: { ...source, match_status: "rejected" },
      history: [{ id: "event-a", action: "reject", actor: "write-test", created_at: "2026-01-01T00:00:00Z" }] };
    return route.fulfill({ json: { ...detail, applied: true } });
  });
  await page.route("**/api/metadata/sources/source-a/restore", (route) => {
    actions.push("restore"); detail = { ...detail, source: { ...source, match_status: "pending" } };
    return route.fulfill({ json: { ...detail, applied: true } });
  });
  await page.goto("/imports/metadata?tab=review");
  await page.getByRole("button", { name: "查看与处理" }).click();
  await page.getByRole("button", { name: "拒绝来源", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "拒绝来源", exact: true });
  await expect(modal.getByRole("textbox")).toHaveCount(0);
  await expect(modal.getByRole("button", { name: "确认处理" })).toBeEnabled();
  await modal.getByRole("button", { name: "确认处理" }).click();
  await expect(modal.getByText(/review save unavailable/)).toBeVisible();
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "确认处理" }).click();
  await expect(modal).not.toBeVisible();
  await page.getByRole("combobox", { name: "来源匹配状态" }).click();
  await page.getByText("已拒绝", { exact: true }).last().click();
  await page.getByRole("button", { name: "查看与处理" }).click();
  await expect(page.getByRole("dialog", { name: "来源详情" }).getByText("write-test", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "恢复待复核", exact: true }).click();
  await page.getByRole("dialog", { name: "恢复待复核", exact: true }).getByRole("button", { name: "确认处理" }).click();
  expect(actions).toEqual(["reject", "restore"]);
});

test("independent admission on mobile retains stale errors and accepts a fresh preview", async ({ page }) => {
  let first = true;
  let removed = false;
  const bodies: object[] = [];
  await page.route("**/api/papers?*", (route) => route.fulfill({ json: { papers: [], total: 0 } }));
  await page.route("**/api/metadata/review?*", (route) => route.fulfill({ json: { total: removed ? 0 : 1, items: removed ? [] : [source], conflicts: [] } }));
  await page.route("**/api/metadata/sources/source-a", (route) => route.fulfill({ json: initial }));
  await page.route("**/api/metadata/sources/source-a/create-paper", (route) => {
    bodies.push(route.request().postDataJSON());
    if (first) { first = false; return route.fulfill({ status: 409, json: { detail: "来源状态已变化" } }); }
    removed = true;
    return route.fulfill({ json: { ...initial, applied: true, created_paper_id: "new-paper" } });
  });
  await page.goto("/imports/metadata?tab=review");
  await page.getByRole("button", { name: "查看与处理" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("Conference publication", { exact: true })).toBeVisible();
  await expect.poll(() => page.getByRole("dialog", { name: "来源详情" }).evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return bounds.left >= -1 && bounds.right <= innerWidth + 1;
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "独立入库", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "独立入库", exact: true });
  await expect(modal.getByRole("textbox")).toHaveCount(0);
  await modal.getByRole("button", { name: "确认处理" }).click();
  await expect(modal.getByText(/来源状态已变化/)).toBeVisible();
  await modal.getByRole("button", { name: /^取\s*消$/ }).click();
  await page.getByRole("button", { name: "刷新详情", exact: true }).click();
  await page.getByRole("button", { name: "独立入库", exact: true }).click();
  await modal.getByRole("button", { name: "确认处理" }).click();
  await expect(page.getByRole("link", { name: "已独立入库，查看论文" })).toHaveAttribute("href", /new-paper/);
  expect(bodies).toHaveLength(2);
});

test("batch preview applies only unique matches and reports per-source failures", async ({ page }) => {
  const entries = [source, { ...source, source_id: "source-b", source_ref: "second-source" }];
  const bodies: { apply: boolean; items: { source_id: string }[] }[] = [];
  await page.route("**/api/metadata/review?*", (route) => route.fulfill({ json: { total: 2, items: entries, conflicts: [] } }));
  await page.route("**/api/metadata/sources/rematch", (route) => {
    const body = route.request().postDataJSON(); bodies.push(body);
    return route.fulfill({ json: body.apply
      ? { applied: 0, items: [{ source_id: "source-a", error: "来源状态已变化", status: 409 }] }
      : { applied: 0, items: [{ ...initial, source_id: "source-a", match: { ...match, paper_id: "paper-a", match_status: "matched" } },
        { ...initial, source_id: "source-b", source: entries[1] }] } });
  });
  await page.goto("/imports/metadata?tab=review");
  await page.getByRole("checkbox", { name: "Select all", exact: true }).check();
  await page.getByRole("button", { name: "重新匹配所选", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "批量重新匹配" });
  await expect(modal.getByText("已预览 2 条来源，可自动关联 1 条", { exact: true })).toBeVisible();
  expect(bodies[0].apply).toBe(false);
  await modal.getByRole("button", { name: "应用唯一匹配（1）" }).click();
  await expect(modal.getByText("来源状态已变化", { exact: true })).toBeVisible();
  expect(bodies[1].items.map((row) => row.source_id)).toEqual(["source-a"]);
});
