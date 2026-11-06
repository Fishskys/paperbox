import { expandNavigation, expect, test } from "./fixtures";
import { readFile } from "node:fs/promises";

const preview = {
  detected: 4,
  total: 3,
  matched: 1,
  created_shell: 1,
  ambiguous: 1,
  unmatched: 0,
  unchanged: 0,
  failed: 1,
  skipped: 0,
  format: "ieee",
  dry_run: true,
  failures: [
    { index: 3, identifier: "broken-record", reason: "title is required" },
  ],
  conflicts: [{ field: "year", kept: 2024, rejected: 2025 }],
  sources: [
    {
      source_ref: "ieee:123",
      match_status: "matched",
      match_method: "identifier:doi",
      paper_id: "paper-1",
      reason: "unique DOI match",
      evidence: ["identifier:doi"],
    },
    {
      source_ref: "ieee:456",
      match_status: "matched",
      match_method: "shell",
      paper_id: null,
    },
    {
      source_ref: "ieee:789",
      match_status: "ambiguous",
      match_method: "title",
      candidates: ["paper-2", "paper-3"],
    },
  ],
};
const file = {
  name: "ieee.jsonl",
  mimeType: "application/x-ndjson",
  buffer: Buffer.from('{"title":"A timing circuit"}\n'),
};

test("metadata options invalidate the preview and the complete report can be downloaded", async ({ page }) => {
  const requests: URL[] = [];
  await page.route("**/api/metadata/import?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    return route.fulfill({ json: { ...preview, dry_run: url.searchParams.get("apply") !== "true" } });
  });
  await page.goto("/imports/metadata");
  await page.locator('input[type="file"]').setInputFiles(file);
  await page.getByRole("button", { name: "预览导入", exact: true }).click();
  const apply = page.getByRole("button", { name: "正式导入", exact: true });
  await expect(apply).toBeEnabled();
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载报告", exact: true }).click();
  const download = await pending;
  const report = JSON.parse(await readFile((await download.path())!, "utf8"));
  expect(report).toEqual(preview);
  await page.getByRole("combobox", { name: "元数据来源类型" }).click();
  await page.getByText("Crossref", { exact: true }).click();
  await expect(apply).toBeDisabled();
  await page.getByRole("spinbutton", { name: "元数据处理条数" }).fill("2");
  await page.getByRole("button", { name: "预览导入", exact: true }).click();
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByText("已处理", { exact: true })).toBeVisible();
  expect(requests.slice(1).every((url) => url.searchParams.get("source_type") === "crossref" && url.searchParams.get("limit") === "2")).toBe(true);
  expect(requests.map((url) => url.searchParams.get("apply"))).toEqual(["false", "false", "true"]);
});

test("metadata import previews before applying and shows record failures and conflicts", async ({
  page,
}) => {
  const requests: {
    apply: string | null;
    authorization?: string;
    contentType: string;
    body: string;
  }[] = [];
  await page.route("**/api/metadata/import?*", (route) => {
    const request = route.request();
    const apply = new URL(request.url()).searchParams.get("apply");
    requests.push({
      apply,
      authorization: request.headers().authorization,
      contentType: request.headers()["content-type"],
      body: request.postDataBuffer()!.toString(),
    });
    return route.fulfill({ json: { ...preview, dry_run: apply !== "true" } });
  });
  await page.goto("/imports/new");
  await expandNavigation(page, "导入");
  await page.getByRole("link", { name: "元数据导入", exact: true }).click();
  await expect(page).toHaveURL(/imports\/metadata/);
  await expect(
    page.getByRole("button", { name: "正式导入", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill("metadata-test-key");
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await page.locator("input[type=file]").setInputFiles(file);
  expect(requests).toHaveLength(0);
  await page.getByRole("button", { name: "预览导入", exact: true }).click();
  await expect(page.getByText("未写入", { exact: true })).toBeVisible();
  await expect(
    page.getByText("将新增 1 篇论文，待补充 PDF 原文", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "详情", exact: true }).first().click();
  await expect(
    page.getByRole("dialog").getByText("unique DOI match"),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "失败 (1)", exact: true }).click();
  await expect(
    page.getByText("title is required", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "冲突 (1)", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "2025", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "正式导入", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "导入报告", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "浏览论文", exact: true }),
  ).toBeVisible();
  expect(requests.map((request) => request.apply)).toEqual(["false", "true"]);
  expect(
    requests.every(
      (request) =>
        request.authorization === "Bearer metadata-test-key" &&
        request.contentType.includes("multipart/form-data; boundary=") &&
        request.body.includes('filename="ieee.jsonl"') &&
        request.body.includes("A timing circuit"),
    ),
  ).toBe(true);
  await expect(
    page.getByRole("button", { name: "正式导入", exact: true }),
  ).toBeDisabled();
  await page
    .locator("input[type=file]")
    .setInputFiles({ ...file, name: "next.json" });
  await expect(page.getByText("尚未预览", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "正式导入", exact: true }),
  ).toBeDisabled();
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage })),
  ).not.toContain("metadata-test-key");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "元数据导入", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("尚未选择元数据文件", { exact: true }),
  ).toBeVisible();
});

test("metadata failures do not enable apply or silently repeat uncertain writes", async ({
  page,
}) => {
  let invalid = true;
  let calls = 0;
  await page.route("**/api/metadata/import?*", (route) => {
    calls++;
    if (invalid)
      return route.fulfill({
        status: 422,
        json: { detail: "invalid JSONL at line 2" },
      });
    if (new URL(route.request().url()).searchParams.get("apply") === "true")
      return route.abort();
    return route.fulfill({ json: preview });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/imports/metadata");
  await page.locator("input[type=file]").setInputFiles(file);
  await page.getByRole("button", { name: "预览导入", exact: true }).click();
  await expect(
    page.getByText("invalid JSONL at line 2", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "正式导入", exact: true }),
  ).toBeDisabled();
  invalid = false;
  await page.getByRole("button", { name: "预览导入", exact: true }).click();
  await expect(page.getByText("未写入", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "正式导入", exact: true }).click();
  await expect(page.getByText(/导入结果未确认/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "正式导入", exact: true }),
  ).toBeDisabled();
  expect(calls).toBe(3);
  await page
    .locator("input[type=file]")
    .setInputFiles({ ...file, name: "unsupported.txt" });
  await expect(
    page.getByText("请选择 JSON、JSONL 或 NDJSON 文件", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "预览导入", exact: true }),
  ).toBeDisabled();
});
