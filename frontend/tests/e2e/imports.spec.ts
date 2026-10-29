import { expect, test } from '@playwright/test';

test('uploads PDFs, follows the accepted job and opens the completed paper', async ({ page }) => {
  await page.route('**/api/papers/ingest/files', (route) => route.fulfill({ status: 202, json: { accepted: 1, duplicate: 0, rejected: 0, results: [{ filename: 'paper.pdf', status: 'accepted', job_id: 'job-1', paper_id: 'paper-1' }] } }));
  const job = { job_id: 'job-1', paper_id: 'paper-1', stage: 'COMPLETED', progress: 100, updated_at: '2026-10-29T08:00:00Z' };
  await page.route('**/api/jobs?*', (route) => route.fulfill({ json: { total: 1, limit: 20, offset: 0, jobs: [job] } }));
  await page.route('**/api/jobs/job-1', (route) => route.fulfill({ json: job }));
  await page.goto('/imports/new');
  await page.locator('input[type=file]').setInputFiles({ name: 'paper.pdf', mimeType: 'application/pdf', buffer: Buffer.from('test pdf') });
  await expect(page.getByText('等待上传', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '开始上传' }).click();
  await expect(page.getByText('已接收', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '查看任务' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog').getByText('已完成', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('link', { name: '打开论文' })).toHaveAttribute('href', '/reading/papers/paper-1');
});

test('data checks are explicit and incomplete results are never shown as healthy', async ({ page }) => {
  let calls = 0;
  await page.route('**/api/consistency?*', (route) => {
    calls++;
    return route.fulfill({ json: { consistent: false, checked_at: '2026-10-29T08:00:00Z', totals: { papers_live: 10, documents_os: 0, objects_minio: 10, problems: 0 }, problems: [], errors: ['OpenSearch unreachable'], truncated: false } });
  });
  await page.goto('/system/data');
  await expect(page.getByText('尚未执行检查')).toBeVisible();
  expect(calls).toBe(0);
  await page.getByRole('button', { name: '开始检查' }).click();
  await expect(page.getByText('结果不完整', { exact: true })).toBeVisible();
  await expect(page.getByText('OpenSearch unreachable')).toBeVisible();
  await expect(page.getByText('数据一致', { exact: true })).toHaveCount(0);
});

test('a credential is sent in headers and is absent from persistent history', async ({ page }) => {
  let authorization: string | undefined;
  await page.route('**/api/papers/ingest', (route) => {
    authorization = route.request().headers().authorization;
    return route.fulfill({ status: 202, json: { job_id: 'url-job', status: 'accepted' } });
  });
  await page.goto('/imports/new');
  await page.getByRole('button', { name: '连接与外观设置' }).click();
  await page.getByLabel('API key', { exact: true }).fill('test-only-key');
  await page.getByRole('button', { name: /^应\s*用$/ }).click();
  await page.getByText('链接导入', { exact: true }).click();
  await page.getByLabel('PDF 链接').fill('https://example.org/paper.pdf');
  await page.getByRole('button', { name: '提交导入' }).click();
  await expect(page.getByRole('link', { name: '查看任务' })).toBeVisible();
  expect(authorization).toBe('Bearer test-only-key');
  expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } })) ).not.toContain('test-only-key');
});

test('task polling stops when tasks complete and preserves the last report on failure', async ({ page }) => {
  let calls = 0;
  let failing = false;
  await page.clock.install();
  await page.route('**/api/jobs?*', (route) => {
    calls++;
    return route.fulfill(failing ? { status: 503, json: { detail: 'temporarily unavailable' } } : { json: { total: 1, jobs: [{ job_id: 'poll-job', stage: calls === 1 ? 'QUEUED' : 'COMPLETED', progress: calls === 1 ? 0 : 100 }] } });
  });
  await page.goto('/imports/jobs');
  await expect(page.getByText('排队中', { exact: true })).toBeVisible();
  await page.clock.fastForward(3100);
  await expect(page.getByText('已完成', { exact: true })).toBeVisible();
  await page.clock.fastForward(30000);
  expect(calls).toBe(2);
  failing = true;
  await page.getByRole('button', { name: /^刷\s*新$/ }).click();
  await expect(page.getByText('temporarily unavailable')).toBeVisible();
  await expect(page.getByText('已完成', { exact: true })).toBeVisible();
});