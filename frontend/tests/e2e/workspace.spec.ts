import { expect, test } from '@playwright/test';

const health = { version: 'test', degraded: false, services: { postgres: 'ok', opensearch: 'unknown', docling: 'disabled' }, dependencies: {} };

test('status is on demand and navigation fits a mobile viewport', async ({ page }) => {
  let probes = 0;
  await page.route('**/health', (route) => { probes++; return route.fulfill({ json: health }); });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/system/status');
  await expect(page.getByRole('heading', { name: '运行状态' })).toBeVisible();
  await expect(page.getByText('未启用', { exact: true })).toBeVisible();
  await expect(page.getByText('未知', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '打开导航' }).click();
  await expect(page.getByRole('dialog').getByRole('link', { name: '运行状态' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '刷新状态' }).click();
  await expect.poll(() => probes).toBe(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('the shell stays available when the API is offline', async ({ page }) => {
  await page.route('**/health', (route) => route.abort());
  await page.goto('/system/status');
  await expect(page.getByText('无法连接 Paperbox，请检查服务后重试')).toBeVisible();
  await expect(page.getByRole('link', { name: 'API 不可达' })).toBeVisible();
});
