import type { Page } from "@playwright/test";

export async function maintenanceFixture(page: Page, report: (input: Record<string, unknown>) => unknown, running = () => false) {
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/maintenance/jobs**", async route => {
    if (route.request().method() === "POST") {
      const input = route.request().postDataJSON(); requests.push(input);
      return route.fulfill({ status: 202, json: { job_id: `task-${requests.length}`, operation: input.operation, dry_run: input.dry_run ?? true, stage: "QUEUED", progress: 0, report_available: false } });
    }
    const id = new URL(route.request().url()).pathname.match(/task-(\d+)/)?.[1];
    const input = requests[Number(id) - 1] || requests.at(-1) || {};
    if (route.request().url().endsWith("/report")) return route.fulfill({ json: await report(input) });
    const active = running();
    return route.fulfill({ json: { job_id: `task-${id || requests.length}`, operation: input.operation, dry_run: input.dry_run ?? true, stage: active ? "PARSING" : "COMPLETED", progress: active ? 50 : 100, report_available: !active } });
  });
  return requests;
}
