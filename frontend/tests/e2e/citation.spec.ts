import { expect, identity, test } from "./fixtures";

for (const width of [1440, 390]) {
  test(`read-only citation copy and exports work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.route("**/api/auth/context", route => route.fulfill({ json: identity("read") }));
    await page.route("**/api/papers/citation-paper", route => route.fulfill({ json: { paper_id: "citation-paper", title: "A Converter", authors: ["Alice Smith"], status: "AWAITING_FILE", files: [] } }));
    const formats: string[] = [];
    await page.route("**/api/papers/citation-paper/citation?*", route => {
      expect(route.request().method()).toBe("GET");
      const format = new URL(route.request().url()).searchParams.get("format")!;
      formats.push(format);
      return route.fulfill({ contentType: "text/plain", body: format === "text" ? "Alice Smith. A Converter. 2020." : format === "bibtex" ? "@misc{paper, title={A Converter}}" : "TY  - GEN\nTI  - A Converter\nER  -\n" });
    });
    await page.goto("/reading/papers/citation-paper");
    await page.getByRole("button", { name: "复制引用", exact: true }).click();
    await expect(page.getByText("引用已复制", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Alice Smith. A Converter. 2020.");
    for (const [label, extension] of [["BibTeX (.bib)", "bib"], ["RIS (.ris)", "ris"]]) {
      const downloading = page.waitForEvent("download");
      await page.getByRole("button", { name: "导出引用", exact: true }).click();
      await page.getByRole("menuitem", { name: label, exact: true }).click();
      expect((await downloading).suggestedFilename()).toBe(`paper-citation-paper.${extension}`);
    }
    expect(formats).toEqual(["text", "bibtex", "ris"]);
    await expect(page.getByRole("link", { name: "论文库管理", exact: true })).toHaveCount(0);
  });
}
