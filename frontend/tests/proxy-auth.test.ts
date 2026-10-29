import { describe, expect, it, vi } from "vitest";
import type { IncomingHttpHeaders } from "node:http";
import { apiProxy, canUseLocalKey } from "../proxy-auth";

function request(headers: IncomingHttpHeaders = {}, address = "127.0.0.1") {
  return {
    headers: { host: "localhost:5173", ...headers },
    socket: { remoteAddress: address },
  };
}

describe("local proxy credential", () => {
  it("allows loopback same-origin requests and local command-line calls", () => {
    expect(canUseLocalKey(request())).toBe(true);
    expect(
      canUseLocalKey(
        request({
          origin: "http://localhost:5173",
          "sec-fetch-site": "same-origin",
        }),
      ),
    ).toBe(true);
    expect(canUseLocalKey(request({ host: "[::1]:5173" }, "::1"))).toBe(true);
    expect(
      canUseLocalKey(request({ host: "127.0.0.1:5173" }, "::ffff:127.0.0.1")),
    ).toBe(true);
  });
  it("preserves explicitly supplied credentials, including invalid ones", () => {
    expect(canUseLocalKey(request({ "x-paperbox-auth-mode": "explicit" }))).toBe(false);
    expect(
      canUseLocalKey(request({ authorization: "Bearer explicit-test-key" })),
    ).toBe(false);
    expect(canUseLocalKey(request({ "x-api-key": "explicit-test-key" }))).toBe(
      false,
    );
  });
  it("does not grant a local credential to remote clients or cross-origin browser requests", () => {
    for (const candidate of [
      request({}, "2001:db8::1"),
      { ...request(), socket: {} },
      request({ host: "untrusted.example:5173" }),
      request({ origin: "https://untrusted.example" }),
      request({ origin: "http://localhost:9999" }),
      request({ origin: "null" }),
      request({ origin: "invalid" }),
      request({ "sec-fetch-site": "cross-site" }),
    ])
      expect(canUseLocalKey(candidate)).toBe(false);
  });
  it("adds the key only to the upstream request", () => {
    const outgoing = { setHeader: vi.fn() };
    let handler: (outgoing: unknown, request: unknown) => void = () => {};
    const proxy = {
      on: vi.fn((_: string, callback: typeof handler) => {
        handler = callback;
      }),
    };
    const options = apiProxy("http://127.0.0.1:8077", "test-only-key");
    options.configure!(proxy as never, options);
    handler(outgoing, request());
    expect(outgoing.setHeader).toHaveBeenCalledWith(
      "Authorization",
      "Bearer test-only-key",
    );
    outgoing.setHeader.mockClear();
    handler(outgoing, request({ origin: "https://untrusted.example" }));
    expect(outgoing.setHeader).not.toHaveBeenCalled();
  });
});
