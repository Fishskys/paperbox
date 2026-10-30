import { expect, test } from "./fixtures";

test("shell original upload confirms the chosen paper and exposes its job", async ({ page }) => {
  const shell = { paper_id: "shell-upload", title: "Imported shell", authors: [], status: "AWAITING_FILE", files: [] };
  let uploads = 0;
  let completed = false;
  await page.route("**/api/jobs/attachment-job", (route) => route.fulfill({ json: { job_id: "attachment-job", stage: completed ? "COMPLETED" : "QUEUED", progress: completed ? 100 : 0 } }));
  await page.route("**/api/papers?*", (route) => route.fulfill({ json: { total: 1, papers: [shell] } }));
  await page.route("**/api/papers/shell-upload", (route) => route.fulfill({ json: completed ? { ...shell, status: "INDEXED", files: [{ mime_type: "application/pdf" }] } : shell }));
  await page.route("**/api/papers/shell-upload/metadata", (route) => route.fulfill({ json: { sources: [], provenance: {} } }));
  await page.route("**/api/papers/shell-upload/file", (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["content-type"]).toContain("multipart/form-data");
    uploads++;
    return route.fulfill({ status: 202, json: { job_id: "attachment-job", paper_id: shell.paper_id, status: "RECEIVED" } });
  });
  await page.goto("/manage/library?status=AWAITING_FILE");
  await page.getByRole("button", { name: "管理论文 Imported shell", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.locator('input[type="file"]').setInputFiles({ name: "my-original.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7 test") });
  const confirmation = page.getByRole("dialog", { name: "补传原文", exact: true });
  await expect(confirmation.getByText("论文：Imported shell", { exact: true })).toBeVisible();
  await expect(confirmation.getByText("文件：my-original.pdf", { exact: true })).toBeVisible();
  expect(uploads).toBe(0);
  await confirmation.getByRole("button", { name: "确认上传", exact: true }).click();
  await expect(drawer.getByText(/原文补传任务已接收/)).toBeVisible();
  await expect(drawer.getByRole("link", { name: "查看任务", exact: true })).toHaveAttribute("href", "/manage/jobs?job=attachment-job");
  await expect(drawer.getByRole("button", { name: "补传原文", exact: true })).toBeDisabled();
  expect(uploads).toBe(1);
  completed = true;
  await expect(drawer.getByText("任务已完成，论文状态已刷新。", { exact: true })).toBeVisible();
  await expect(drawer.getByText("待补充 PDF 原文", { exact: true })).toHaveCount(0);
  await page.goto("/library");
  await expect(page.getByRole("button", { name: "补传原文", exact: true })).toHaveCount(0);
});

test("management filters shell and processing states and preserves filters on reload", async ({ page }) => {
  const requests: URL[] = [];
  await page.route("**/api/papers?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    return route.fulfill({ json: {
      total: 100,
      papers: [{ paper_id: "shell", title: "Clock shell", authors: ["Chen"], status: url.searchParams.get("status"), files: [] }],
    } });
  });
  await page.goto("/manage/library?q=clock&author=Chen&page=3");
  const state = page.getByRole("combobox", { name: "处理状态" });
  await state.click();
  await page.getByText("待补原文（空壳论文）", { exact: true }).click();
  await expect(page).toHaveURL(/status=AWAITING_FILE/);
  expect(new URL(page.url()).searchParams.has("page")).toBe(false);
  await expect(page.getByRole("button", { name: "管理论文 Clock shell", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("待补原文（空壳论文）", { exact: true })).toBeVisible();
  for (const [label, value] of [["待处理", "PENDING"], ["处理中", "PROCESSING"], ["失败", "FAILED"]]) {
    await state.click();
    await page.locator(".ant-select-item-option-content").getByText(label, { exact: true }).click();
    await expect.poll(() => requests.at(-1)?.searchParams.get("status")).toBe(value);
  }
  expect(requests.every((url) => url.searchParams.get("q") === "clock" && url.searchParams.get("author") === "Chen")).toBe(true);
});

test("library management edits only changed fields and previews destructive actions", async ({
  page,
}) => {
  let paper = {
    paper_id: "managed",
    title: "Original title",
    authors: ["Chen"],
    doi: "10.1234/example",
    year: 2024,
    status: "INDEXED",
    files: [{ mime_type: "application/pdf" }],
  };
  const writes: { method: string; path: string; body: unknown }[] = [];
  let deleted = false;
  await page.route("**/api/papers?*", (route) =>
    route.fulfill({
      json: { total: deleted ? 0 : 1, papers: deleted ? [] : [paper] },
    }),
  );
  await page.route("**/api/papers/managed", (route) => {
    if (route.request().method() === "DELETE") {
      deleted = true;
      writes.push({ method: "DELETE", path: "paper", body: null });
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json: paper });
  });
  await page.route("**/api/papers/managed/metadata*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "PATCH") {
      const body = request.postDataJSON();
      writes.push({ method: "PATCH", path: "metadata", body });
      paper = { ...paper, ...body };
      return route.fulfill({
        json: { fields: Object.keys(body), rejected: [] },
      });
    }
    if (request.method() === "DELETE") {
      writes.push({ method: "DELETE", path: url.search, body: null });
      if (url.searchParams.get("dry_run") === "false")
        paper = { ...paper, title: "", authors: [], doi: "" };
      return route.fulfill({
        json: { source_rows: 2, provenance_rows: 5, identifier_rows: 1 },
      });
    }
    return route.fulfill({
      json: {
        paper_id: "managed",
        values: {},
        sources: [
          {
            source_id: "source",
            source_type: "manual",
            source_ref: "manual:one",
          },
        ],
        provenance: {
          title: [
            {
              provenance_id: "claim",
              value: paper.title,
              is_current: true,
              decided_by: "manual",
            },
          ],
        },
      },
    });
  });
  await page.goto("/manage/library");
  await page
    .getByRole("button", { name: "管理论文 Original title", exact: true })
    .click();
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByLabel("标题", { exact: true }).fill("Updated title");
  await drawer.getByRole("button", { name: "保存元数据", exact: true }).click();
  await expect(drawer.getByText(/元数据已保存/)).toBeVisible();
  expect(writes[0]).toEqual({
    method: "PATCH",
    path: "metadata",
    body: { title: "Updated title" },
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () => {
      const box = await drawer.boundingBox();
      return !!box && box.x >= 0 && box.x + box.width <= 391;
    })
    .toBe(true);
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(
    drawer.getByRole("heading", { name: "Updated title", exact: true }),
  ).toBeVisible();
  await drawer.getByRole("tab", { name: "来源与历史", exact: true }).click();
  await expect(drawer.getByText("manual:one", { exact: true })).toBeVisible();
  await drawer.getByRole("tab", { name: "操作", exact: true }).click();
  await drawer.getByRole("button", { name: "清除元数据", exact: true }).click();
  const confirmation = page.getByRole("dialog", {
    name: "清除元数据",
    exact: true,
  });
  await expect(
    confirmation.getByText("字段历史", { exact: true }),
  ).toBeVisible();
  await confirmation.getByRole("button", { name: /^取\s*消$/ }).click();
  expect(
    writes.filter((write) => write.path === "?dry_run=false"),
  ).toHaveLength(0);
  await drawer.getByRole("button", { name: "清除元数据", exact: true }).click();
  await confirmation
    .getByRole("button", { name: "确认执行", exact: true })
    .click();
  await expect(
    drawer.getByRole("heading", { name: "未命名论文", exact: true }),
  ).toBeVisible();
  expect(
    writes.filter((write) => write.path === "?dry_run=false"),
  ).toHaveLength(1);
  await drawer.getByRole("tab", { name: "操作", exact: true }).click();
  await drawer.getByRole("button", { name: "删除论文", exact: true }).click();
  await page
    .getByRole("dialog", { name: "删除论文", exact: true })
    .getByRole("button", { name: "确认执行", exact: true })
    .click();
  await expect(drawer).toHaveCount(0);
  await expect(page.getByText("暂无论文", { exact: true })).toBeVisible();
  expect(writes.at(-1)).toEqual({
    method: "DELETE",
    path: "paper",
    body: null,
  });
});

test("single-paper maintenance previews extraction and queues an inspectable reindex task", async ({
  page,
}) => {
  const paper = {
    paper_id: "managed",
    title: "Maintenance paper",
    authors: [],
    status: "INDEXED",
    files: [{ mime_type: "application/pdf" }],
  };
  await page.route("**/api/papers?*", (route) =>
    route.fulfill({ json: { total: 1, papers: [paper] } }),
  );
  await page.route("**/api/papers/managed", (route) =>
    route.fulfill({ json: paper }),
  );
  await page.route("**/api/papers/managed/metadata", (route) =>
    route.fulfill({ json: { sources: [], provenance: {} } }),
  );
  const extraction: boolean[] = [];
  await page.route("**/api/papers/managed/metadata/reextract", (route) => {
    const dry = route.request().postDataJSON().dry_run;
    extraction.push(dry);
    return route.fulfill({
      json: {
        dry_run: dry,
        changed_papers: 1,
        failed: 0,
        changes: [{ field: "year", new: 2025 }],
        errors: [],
      },
    });
  });
  await page.route("**/api/papers/managed/reindex", (route) =>
    route.fulfill({
      status: 202,
      json: { job_id: "rebuild-job", status: "QUEUED" },
    }),
  );
  await page.goto("/manage/library?paper=managed");
  const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
  await drawer.getByRole("tab", { name: "操作", exact: true }).click();
  await drawer
    .getByRole("button", { name: "重新提取元数据", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog", { name: "重新提取元数据", exact: true })
      .getByText("2025", { exact: true }),
  ).toBeVisible();
  expect(extraction).toEqual([true]);
  await page
    .getByRole("dialog", { name: "重新提取元数据", exact: true })
    .getByRole("button", { name: "确认执行", exact: true })
    .click();
  await expect(
    drawer.getByText("已更新 1 篇，失败 0 篇", { exact: true }),
  ).toBeVisible();
  expect(extraction).toEqual([true, false]);
  await drawer.getByRole("tab", { name: "操作", exact: true }).click();
  await drawer.getByRole("button", { name: "重建索引", exact: true }).click();
  await page
    .getByRole("dialog", { name: "重建索引", exact: true })
    .getByRole("button", { name: "确认执行", exact: true })
    .click();
  await expect(
    drawer.getByRole("link", { name: "查看任务", exact: true }),
  ).toHaveAttribute("href", "/manage/jobs?job=rebuild-job");
});

test("metadata-only papers do not request a missing PDF", async ({ page }) => {
  let fileCalls = 0;
  await page.route("**/api/papers/shell", (route) =>
    route.fulfill({
      json: {
        paper_id: "shell",
        title: "Pending original",
        authors: [],
        status: "AWAITING_FILE",
        files: [],
      },
    }),
  );
  await page.route("**/api/papers/shell/file", (route) => {
    fileCalls++;
    return route.fulfill({ status: 404 });
  });
  await page.goto("/reading/papers/shell");
  await expect(
    page.getByText("待补充 PDF 原文", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "下载原文", exact: true }),
  ).toBeDisabled();
  expect(fileCalls).toBe(0);
});
