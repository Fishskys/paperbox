import { createRequire } from "node:module";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

// Exercise the real browser, proxy and API using preview only.
const root = resolve(import.meta.dirname, "..");
const origin = process.env.WEBUI_URL || "http://127.0.0.1:5173";
const require = createRequire(
  new URL("../frontend/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");
const fixtureRoot = resolve(root, "tests/metadata");
const expected = (
  await readFile(resolve(fixtureRoot, "ieee_metadata_example.jsonl"), "utf8")
)
  .split(/\r?\n/)
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line)).length;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (
      ["GET", "HEAD"].includes(request.method()) ||
      (request.method() === "POST" &&
        url.pathname === "/api/metadata/import" &&
        url.searchParams.get("apply") === "false")
    )
      return route.continue();
    return route.fulfill({
      status: 409,
      json: { detail: "Live acceptance permits metadata preview only" },
    });
  });
  const countPapers = async () => {
    const response = await page.request.get(`${origin}/api/papers?limit=1`);
    if (!response.ok())
      throw new Error(`Library unavailable: HTTP ${response.status()}`);
    return (await response.json()).total;
  };
  const before = await countPapers();
  await page.goto(`${origin}/imports/metadata`);
  const reports = [];
  for (const name of [
    "ieee_metadata_example.jsonl",
    "ieee_metadata_example.json",
  ]) {
    await page
      .locator("input[type=file]")
      .setInputFiles(resolve(fixtureRoot, name));
    const pending = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/metadata/import" &&
        response.request().method() === "POST",
      { timeout: 120000 },
    );
    await page.getByRole("button", { name: "预览导入", exact: true }).click();
    const response = await pending;
    const report = await response.json();
    if (!response.ok())
      throw new Error(
        `${name}: HTTP ${response.status()}: ${JSON.stringify(report)}`,
      );
    if (
      report.dry_run !== true ||
      report.detected !== expected ||
      report.failed !== 0
    )
      throw new Error(
        `${name}: unexpected preview: ${JSON.stringify({ dry_run: report.dry_run, detected: report.detected, failed: report.failed })}`,
      );
    await page
      .getByRole("heading", { name: "导入预览", exact: true })
      .waitFor();
    await page.getByText("未写入", { exact: true }).waitFor();
    reports.push({
      file: name,
      status: response.status(),
      detected: report.detected,
      failed: report.failed,
      dry_run: report.dry_run,
    });
  }
  const after = await countPapers();
  if (before !== after)
    throw new Error(
      `Paper count changed during preview: ${before} -> ${after}`,
    );
  if (errors.length) throw new Error(errors.join("\n"));
  const pendingReview = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/metadata/review",
  );
  await page.getByRole("tab", { name: "元数据复核", exact: true }).click();
  const reviewResponse = await pendingReview;
  const review = await reviewResponse.json();
  if (
    !reviewResponse.ok() ||
    !Array.isArray(review.items) ||
    !Array.isArray(review.conflicts)
  )
    throw new Error(`Review unavailable: HTTP ${reviewResponse.status()}`);
  await page
    .getByRole("heading", { name: "待复核来源", exact: true })
    .waitFor();
  await mkdir(resolve(root, "logs/codex"), { recursive: true });
  await page.screenshot({
    path: resolve(root, "logs/codex/webui-metadata-live.png"),
    fullPage: true,
  });
  console.log(
    JSON.stringify({
      origin,
      status: "passed",
      papers_before: before,
      papers_after: after,
      reports,
      review: {
        status: reviewResponse.status(),
        sources: review.items.length,
        conflicts: review.conflicts.length,
      },
    }),
  );
} finally {
  await browser.close();
}
