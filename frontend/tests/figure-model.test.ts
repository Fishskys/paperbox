import { afterEach, describe, expect, it, vi } from "vitest";
import { setCredential } from "../src/api";
import { figureArchive } from "../src/figure-model";

afterEach(() => { setCredential(""); vi.unstubAllGlobals(); });
describe("figure archive download", () => {
  it("pins the published generation and sends credentials only in headers", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response("zip", { headers: { "Content-Type": "application/zip" } }));
    vi.stubGlobal("fetch", fetcher);
    setCredential("test-figure-key");
    expect((await figureArchive("paper-1", "generation-1")).size).toBe(3);
    const [path, options] = fetcher.mock.calls[0];
    expect(path).toBe("/api/papers/paper-1/figures/download?generation_id=generation-1&variant=all");
    expect(options.headers.Authorization).toBe("Bearer test-figure-key");
  });
  it("does not request an unpinned archive that could trigger extraction", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    await expect(figureArchive("paper-1", "")).rejects.toThrow("暂无可下载");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("does not turn a pending job response into a downloaded ZIP", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"job_id":"job-1"}', { status: 202 })));
    await expect(figureArchive("paper-1", "generation-1")).rejects.toThrow("尚未就绪");
  });
  it("preserves generation-change errors and does not retry", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"detail":{"code":"FIGURE_GENERATION_CHANGED","message":"generation changed"}}', { status: 409 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(figureArchive("paper-1", "generation-1")).rejects.toThrow("generation changed");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("rejects a non-ZIP success response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { headers: { "Content-Type": "application/json" } })));
    await expect(figureArchive("paper-1", "generation-1")).rejects.toThrow("服务未返回附图压缩包");
  });
});
