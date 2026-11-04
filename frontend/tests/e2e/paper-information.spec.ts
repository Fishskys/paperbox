import { expect, identity, test } from "./fixtures";

const paper = {
  paper_id: "info-paper", title: "A time-to-digital converter with calibrated resolution and a wide measurement range",
  authors: ["Alice Chen", "Bob Li", "Christina Alexandra Montgomery"], year: 2020,
  venue: "IEEE Journal of Solid-State Circuits", paper_type: "journal", volume: "55", issue: "4", pages: "100–110",
  doi: "10.1234/a-very-long-identifier-without-spaces-for-overflow-checking", abstract: "A converter with calibrated resolution.\nA second paragraph.",
  status: "AWAITING_FILE", files: [],
};

for (const width of [1440, 390, 320]) {
  test(`paper information labels and citation actions are clear at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.route("**/api/auth/context", route => route.fulfill({ json: identity("read") }));
    await page.route("**/api/papers?*", route => route.fulfill({ json: { total: 1, papers: [paper], facets: {} } }));
    await page.route(`**/api/papers/${paper.paper_id}`, route => route.fulfill({ json: paper }));
    await page.route(`**/api/papers/${paper.paper_id}/citation?*`, route => route.fulfill({ contentType: "text/plain", body: "Alice Chen. A converter. 2020." }));
    for (const path of ["/search/standard", `/reading/papers/${paper.paper_id}`]) {
      await page.goto(path);
      await page.getByRole("button", { name: path === "/search/standard" ? `论文信息 ${paper.title}` : "论文信息", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "论文信息", exact: true });
      await expect(dialog.getByRole("heading", { name: paper.title, exact: true })).toBeVisible();
      await expect(dialog.getByText("作者", { exact: true })).toBeVisible();
      await expect(dialog.locator(".paper-info-authors li")).toHaveText(paper.authors);
      const publication = dialog.getByRole("region", { name: "出版信息", exact: true });
      for (const label of ["期刊 / 会议", "发表年份", "文献类型", "卷号", "期号", "页码", "DOI"]) {
        await expect(publication.getByText(label, { exact: true })).toBeVisible();
      }
      await expect(publication.getByText(paper.venue, { exact: true })).toBeVisible();
      await expect(publication.getByText("期刊论文", { exact: true })).toBeVisible();
      await expect(dialog.getByRole("region", { name: "摘要", exact: true })).toContainText(paper.abstract);
      const citation = dialog.getByRole("region", { name: "引用", exact: true });
      await expect(citation.getByRole("button", { name: "复制引用", exact: true })).toHaveText("复制引用");
      await expect(citation.getByRole("button", { name: "导出引用", exact: true })).toHaveText("导出引用");
      await citation.getByRole("button", { name: "复制引用", exact: true }).click();
      await expect(page.getByText("引用已复制", { exact: true })).toBeVisible();
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Alice Chen. A converter. 2020.");
      const download = page.waitForEvent("download");
      await citation.getByRole("button", { name: "导出引用", exact: true }).click();
      await page.getByRole("menuitem", { name: "RIS (.ris)", exact: true }).click();
      expect((await download).suggestedFilename()).toBe("paper-info-paper.ris");
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      for (const button of await citation.getByRole("button").all()) {
        const box = await button.boundingBox();
        expect(box).not.toBeNull();
        expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      }
      await page.keyboard.press("Escape");
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
  });
}

test("paper information explicitly identifies missing metadata", async ({ page }) => {
  await page.route("**/api/papers/empty-info", route => route.fulfill({ json: { paper_id: "empty-info", title: "Metadata only", authors: [], status: "AWAITING_FILE", files: [] } }));
  await page.goto("/reading/papers/empty-info");
  await page.getByRole("button", { name: "论文信息", exact: true }).click();
  const info = page.getByRole("dialog", { name: "论文信息", exact: true });
  await expect(info.locator(".paper-info-authors dd")).toHaveText("未标注");
  await expect(info.locator(".paper-info-fields dd")).toHaveText(["未标注", "未标注", "未标注", "未标注"]);
  await expect(info.getByText("暂无摘要", { exact: true })).toBeVisible();
});
