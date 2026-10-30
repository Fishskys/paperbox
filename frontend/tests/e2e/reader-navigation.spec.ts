import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "./fixtures";

test("reader uses real outline, lazy thumbnails, text search and read-only authenticated figures", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const calls: string[] = [];
  let figureAuth = "";
  let archiveQuery = "";
  await page.route("**/api/papers/paper-1", (route) => route.fulfill({ json: { paper_id: "paper-1", title: "Attention Is All You Need", status: "INDEXED", authors: [] } }));
  await page.route("**/api/papers/paper-1/file", (route) => route.fulfill({ contentType: "application/pdf", body: readFileSync(resolve("../tests/fixtures/smoke_sample.pdf")) }));
  await page.route("**/api/papers/paper-1/figures**", (route) => {
    calls.push(route.request().method() + " " + new URL(route.request().url()).pathname);
    if (new URL(route.request().url()).pathname.endsWith("/download")) {
      archiveQuery = new URL(route.request().url()).search;
      return route.fulfill({ contentType: "application/zip", body: Buffer.from("PK-test-archive") });
    }
    if (route.request().url().includes("/figure-1/file")) {
      figureAuth = route.request().headers().authorization;
      return route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6WH0AAAAASUVORK5CYII=", "base64") });
    }
    return route.fulfill({ json: { status: "completed", generation_id: "read-generation", job_id: null, total: 1, figures: [{ figure_id: "figure-1", display_name: "Figure 1", caption_text: "Transformer", match_status: "needs_review", native_format: "png", native_url: "/api/papers/paper-1/figures/figure-1/file?variant=native", parts: [{ part: 1, page: 3, preview_url: "/api/papers/paper-1/figures/figure-1/file?variant=preview" }] }] } });
  });
  await page.goto("/reading/papers/paper-1");
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill("reader-key");
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await expect(page.getByRole("button", { name: "阅读导航", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "阅读导航", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "阅读导航", exact: true });
  await expect(drawer.getByRole("link", { name: "Introduction", exact: true })).toBeVisible();
  await drawer.getByRole("link", { name: "Introduction", exact: true }).click();
  await expect(page).toHaveURL(/page=2/);
  await drawer.getByText("缩略图", { exact: true }).click();
  await expect(drawer.locator("canvas").first()).toBeVisible();
  expect(await drawer.locator("canvas").count()).toBeLessThan(15);
  await drawer.getByRole("button", { name: "缩略图第 1 页", exact: true }).click();
  await expect(page).toHaveURL(/page=1/);
  await drawer.getByText("查找", { exact: true }).click();
  await drawer.getByRole("searchbox", { name: "文内查找" }).fill("Attention");
  await drawer.getByRole("searchbox", { name: "文内查找" }).press("Enter");
  await expect(drawer.locator(".pdf-text-match").first()).toBeVisible();
  await expect(page.locator(".pdf-sheet mark").first()).toHaveText(/Attention/i);
  await drawer.getByText("附图", { exact: true }).click();
  await expect(drawer.getByText("待复核", { exact: true })).toBeVisible();
  await expect(drawer.getByRole("img", { name: "Figure 1 1" })).toBeVisible();
  expect(figureAuth).toBe("Bearer reader-key");
  await expect(drawer.getByRole("button", { name: "提取附图", exact: true })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "删除全部附图", exact: true })).toHaveCount(0);
  const download = page.waitForEvent("download");
  await drawer.getByRole("button", { name: "下载全部附图", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("paper-1-figures.zip");
  expect(archiveQuery).toBe("?generation_id=read-generation&variant=all");
  await drawer.getByRole("button", { name: "第 3 页", exact: true }).click();
  await expect(page).toHaveURL(/page=3/);
  expect(calls.every((value) => value.startsWith("GET ") && !value.includes("/extract"))).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await drawer.boundingBox(); return !!box && box.x >= 0 && box.x + box.width <= 391; }).toBe(true);
  expect(errors).toEqual([]);
});
