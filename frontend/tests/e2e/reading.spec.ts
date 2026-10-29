import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const paper = {
  paper_id: "paper-1",
  title: "A low-power time-to-digital converter",
  authors: ["Y. Chen", "M. Li"],
  year: 2025,
  venue: "ISSCC",
  abstract: "A timing circuit with calibrated resolution.",
  status: "INDEXED",
};
const results = {
  total: 1,
  took_ms: 18,
  results: [
    {
      ...paper,
      evidence: [
        {
          chunk_id: "chunk-1",
          page: 3,
          section: "Circuit implementation",
          text: "The proposed time-to-digital converter measures the delay using a calibrated clock.",
        },
      ],
    },
  ],
};

test("search evidence opens a real rendered PDF page and returns to the query", async ({
  page,
}) => {
  const errors: string[] = [];
  let pdfCredential: string | undefined;
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/search", (route) =>
    route.fulfill({ json: results }),
  );
  await page.route("**/api/papers/paper-1", (route) =>
    route.fulfill({ json: paper }),
  );
  await page.route("**/api/papers/paper-1/file", (route) => {
    pdfCredential = route.request().headers().authorization;
    return route.fulfill({
      contentType: "application/pdf",
      body: readFileSync(resolve("../tests/fixtures/smoke_sample.pdf")),
    });
  });
  await page.goto("/search");
  await page.getByRole("button", { name: "连接与外观设置" }).click();
  await page.getByLabel("API key", { exact: true }).fill("read-test-only");
  await page.getByRole("button", { name: /^应\s*用$/ }).click();
  await page.getByRole("textbox", { name: "查询内容" }).fill("time-to-digital");
  await page.getByRole("button", { name: "搜索论文" }).click();
  await expect(
    page.getByRole("link", { name: paper.title, exact: true }),
  ).toBeVisible();
  await page.getByText("1 条命中证据", { exact: true }).click();
  await page.getByRole("link", { name: "第 3 页" }).click();
  await expect(page).toHaveURL(/reading\/papers\/paper-1\?page=3/);
  await expect(page.getByRole("spinbutton", { name: "阅读页码" })).toHaveValue(
    "3",
  );
  const canvas = page.locator(".react-pdf__Page canvas");
  await expect(canvas).toBeVisible();
  await expect
    .poll(() =>
      canvas.evaluate((element: HTMLCanvasElement) => {
        const pixels = element
          .getContext("2d")!
          .getImageData(0, 0, element.width, element.height).data;
        let ink = 0;
        for (let i = 0; i < pixels.length; i += 40)
          if (pixels[i] < 180 && pixels[i + 3] > 0) ink++;
        return ink;
      }),
    )
    .toBeGreaterThan(100);
  expect(pdfCredential).toBe("Bearer read-test-only");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(canvas).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.getByRole("button", { name: "返回来源页面" }).click();
  await expect(page).toHaveURL(/search\?q=time-to-digital/);
  await expect(page.getByRole("textbox", { name: "查询内容" })).toHaveValue(
    "time-to-digital",
  );
  await page.getByRole("link", { name: "最近阅读", exact: true }).click();
  await expect(page.getByRole("link", { name: paper.title })).toHaveAttribute(
    "href",
    "/reading/papers/paper-1?page=3",
  );
  expect(errors).toEqual([]);
});

test("mobile search results fit the work area", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/search", (route) =>
    route.fulfill({ json: results }),
  );
  await page.goto("/search?q=clock");
  await expect(
    page.getByRole("link", { name: paper.title, exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("late search responses do not overwrite the current query", async ({
  page,
}) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/search", async (route) => {
    const query = route.request().postDataJSON().query;
    if (query === "slow") await gate;
    await route
      .fulfill({
        json: {
          ...results,
          results: [{ ...results.results[0], title: query }],
        },
      })
      .catch(() => {});
  });
  await page.goto("/search?q=slow");
  await page
    .getByRole("link", { name: "检索记录", exact: true })
    .first()
    .click();
  await page.goto("/search?q=fast");
  await expect(
    page.getByRole("link", { name: "fast", exact: true }),
  ).toBeVisible();
  release();
  await expect(
    page.getByRole("link", { name: "slow", exact: true }),
  ).toHaveCount(0);
});
