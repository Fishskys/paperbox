import { expect, test } from "./fixtures";
import { contrastFailures } from "./contrast";

const paper = { paper_id: "appearance-paper", title: "A precision timing converter", authors: ["Alice Chen", "Bob Li"], year: 2020, venue: "IEEE Journal of Solid-State Circuits", doi: "10.1234/timing", abstract: "A converter with calibrated resolution.", status: "AWAITING_FILE", files: [] };

for (const appearance of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    test(`${appearance} pages have readable information at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(value => localStorage.setItem("paperbox:theme", value), appearance);
      await page.route("**/api/**", route => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/api/auth/context" || path === "/api/jobs/queue") return route.fallback();
        if (path === "/api/papers") return route.fulfill({ json: { total: 1, papers: [paper], facets: {} } });
        if (path === `/api/papers/${paper.paper_id}`) return route.fulfill({ json: paper });
        if (path === "/api/jobs") return route.fulfill({ json: { total: 0, jobs: [] } });
        if (path === "/api/metadata/review") return route.fulfill({ json: { total: 0, sources: [], conflicts: [], conflicts_total: 0 } });
        return route.fulfill({ status: 503, json: { detail: "测试服务暂不可用" } });
      });
      await page.route("**/health", route => route.fulfill({ json: { version: "0.9.0", degraded: true, services: { postgres: "ok", opensearch: "error", minio: "disabled", embedding: "unknown" } } }));
      const routes = ["/search", "/search/standard", "/search/history", "/reading/recent", `/reading/papers/${paper.paper_id}`, "/imports/new", "/imports/metadata", "/manage/library", "/manage/jobs", "/system/status", "/system/data", "/system/index", "/system/metadata"];
      const results: Record<string, string[]> = {};
      for (const path of routes) {
        await page.goto(path);
        await expect(page.locator("main h1")).toBeVisible();
        await expect(page.locator(".ant-skeleton")).toHaveCount(0);
        await expect(page.getByText("PAPERBOX WORKSPACE", { exact: true })).toHaveCount(0);
        // Finish component entrance transitions before sampling computed colors.
        await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
        results[path] = await contrastFailures(page);
        if (path === "/search/standard") {
          await page.getByRole("button", { name: `论文信息 ${paper.title}` }).click();
          await expect(page.getByRole("dialog")).toBeVisible();
          results[`${path}/info`] = await contrastFailures(page);
          await page.screenshot({ path: testInfo.outputPath("paper-info.png"), fullPage: true });
        }
        if (path.startsWith("/reading/papers/")) {
          await page.getByRole("button", { name: "论文信息", exact: true }).click();
          await expect(page.getByRole("dialog", { name: "论文信息", exact: true })).toBeVisible();
          results[`${path}/info`] = await contrastFailures(page);
        }
      }
      if (width === 390) await page.getByRole("button", { name: "打开导航" }).click();
      await page.getByRole("button", { name: "连接与外观设置" }).click();
      await expect(page.getByRole("dialog", { name: "连接与外观", exact: true })).toBeVisible();
      results.settings = await contrastFailures(page);
      await testInfo.attach("contrast-audit", { body: JSON.stringify(results, null, 2), contentType: "application/json" });
      expect(Object.fromEntries(Object.entries(results).filter(([, failures]) => failures.length))).toEqual({});
    });
  }
}
