import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request, setCredential } from "../src/api";

afterEach(() => {
  setCredential("");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe("API boundary", () => {
  it("uses the longer metadata timeout without changing the default request deadline", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response("{}")),
    );
    await request(
      "/api/metadata/import",
      { method: "POST", body: new FormData() },
      120000,
    );
    expect(timeout).toHaveBeenLastCalledWith(120000);
    await request("/api/papers");
    expect(timeout).toHaveBeenLastCalledWith(30000);
  });
  it("recognizes an HTML gateway failure without pretending the API is online", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<h1>Bad Gateway</h1>", { status: 502 }),
        ),
    );
    await expect(request("/api/papers")).rejects.toThrow(
      "后端不可达或网关异常",
    );
  });
  it("keeps credentials in request headers and preserves status errors", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "insufficient role" }), {
        status: 403,
      }),
    );
    vi.stubGlobal("fetch", fetcher);
    setCredential("test-only");
    await expect(request("/api/papers")).rejects.toMatchObject({ status: 403 });
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe(
      "Bearer test-only",
    );
  });
  it("does not reinterpret a transport failure as an empty result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")));
    await expect(request("/api/papers")).rejects.toThrow("无法连接");
  });
  it("retains retry-after from backpressure responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('{"detail":"busy"}', {
          status: 429,
          headers: { "Retry-After": "5" },
        }),
      ),
    );
    await expect(request("/api/papers")).rejects.toEqual(
      new ApiError(429, "busy", "5"),
    );
  });
});
