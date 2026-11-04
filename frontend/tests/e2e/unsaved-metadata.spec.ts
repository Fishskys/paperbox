import { expect, test } from "./fixtures";

for (const width of [1440, 390]) {
  test(`unsaved metadata can be kept, undone or discarded at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const paper = { paper_id: "unsaved", title: "Original title", authors: [], status: "AWAITING_FILE", files: [] };
    await page.route("**/api/papers?*", route => route.fulfill({ json: { total: 1, papers: [paper] } }));
    await page.route("**/api/papers/unsaved", route => route.fulfill({ json: paper }));
    await page.route("**/api/papers/unsaved/metadata", route => route.fulfill({ json: { sources: [], provenance: {} } }));
    await page.goto("/manage/library");
    await page.getByRole("button", { name: "管理论文 Original title", exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "管理论文", exact: true });
    const title = drawer.getByRole("textbox", { name: /标题/ });
    await title.fill("Changed title");
    await drawer.getByRole("link", { name: "查看相关任务", exact: true }).click();
    const confirm = page.getByRole("dialog", { name: "有未保存的元数据", exact: true });
    await confirm.getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(title).toHaveValue("Changed title");
    await drawer.getByRole("button", { name: "撤销编辑", exact: true }).click();
    await expect(title).toHaveValue("Original title");
    await expect(drawer.getByRole("button", { name: "撤销编辑", exact: true })).toBeDisabled();
    await title.fill("Changed again");
    await page.goBack();
    await expect(confirm).toBeVisible();
    await confirm.getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(title).toHaveValue("Changed again");
    await drawer.locator(".ant-drawer-close").click();
    await confirm.getByRole("button", { name: "丢弃修改", exact: true }).click();
    await expect(drawer).toHaveCount(0);
    await page.getByRole("button", { name: "管理论文 Original title", exact: true }).click();
    await expect(title).toHaveValue("Original title");
    await drawer.locator(".ant-drawer-close").click();
    await expect(drawer).toHaveCount(0);
    await expect(confirm).toHaveCount(0);
  });
}
