import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

// Read-only acceptance against an explicitly running UI and its real API.
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const require = createRequire(
  new URL("../frontend/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");
const output = resolve(import.meta.dirname, "../logs/codex");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  for (const [width, height] of [
    [1440, 900],
    [1280, 800],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.goto(`${origin}/library`);
    await page.locator(".ant-table-tbody .paper-cell a").first().waitFor();
    if (
      !(await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ))
    )
      throw new Error(`Library overflows at ${width}`);
    await page.screenshot({
      path: resolve(output, `webui-library-${width}.png`),
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator(".ant-table-tbody .paper-cell a").first().click();
  const canvas = page.locator(".react-pdf__Page canvas").first();
  await canvas.waitFor({ state: "visible", timeout: 30000 });
  await page.waitForFunction(() => {
    const element = document.querySelector(".react-pdf__Page canvas");
    if (!element) return false;
    const pixels = element
      .getContext("2d")
      .getImageData(0, 0, element.width, element.height).data;
    let ink = 0;
    for (let i = 0; i < pixels.length; i += 40)
      if (pixels[i] < 180 && pixels[i + 3] > 0) ink++;
    return ink > 100;
  });
  await page.screenshot({ path: resolve(output, "webui-reader-1440.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => {
    const element = document.querySelector(".react-pdf__Page canvas");
    if (!element || element.getBoundingClientRect().width > innerWidth - 32)
      return false;
    const pixels = element
      .getContext("2d")
      .getImageData(0, 0, element.width, element.height).data;
    let ink = 0;
    for (let i = 0; i < pixels.length; i += 40)
      if (pixels[i] < 180 && pixels[i + 3] > 0) ink++;
    return ink > 100;
  });
  await page.screenshot({ path: resolve(output, "webui-reader-390.png") });
  if (
    !(await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ))
  )
    throw new Error("Reader overflows on mobile");
  if (errors.length) throw new Error(errors.join("\n"));
  process.stdout.write(
    JSON.stringify({
      origin,
      status: "passed",
      viewports: [1440, 1280, 390],
      reader: page.url(),
      output,
    }) + "\n",
  );
} finally {
  await browser.close();
}
