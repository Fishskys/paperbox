import type { IncomingHttpHeaders } from "node:http";
import type { ProxyOptions } from "vite";

type ClientRequest = {
  headers: IncomingHttpHeaders;
  socket: { remoteAddress?: string };
};

export function canUseLocalKey(request: ClientRequest): boolean {
  if (request.headers["x-paperbox-auth-mode"] === "explicit") return false;
  if (request.headers.authorization || request.headers["x-api-key"])
    return false;
  if (
    !["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(
      request.socket.remoteAddress || "",
    )
  )
    return false;
  try {
    const host = new URL(`http://${request.headers.host || ""}`);
    if (!["localhost", "127.0.0.1", "[::1]"].includes(host.hostname))
      return false;
    const origin = request.headers.origin;
    if (origin) {
      const source = new URL(origin);
      if (
        !["http:", "https:"].includes(source.protocol) ||
        source.host !== host.host
      )
        return false;
    }
    return request.headers["sec-fetch-site"] !== "cross-site";
  } catch {
    return false;
  }
}

export function apiProxy(target: string, key: string): ProxyOptions {
  return {
    target,
    configure(proxy) {
      proxy.on("proxyReq", (outgoing, request) => {
        if (key && canUseLocalKey(request))
          outgoing.setHeader("Authorization", `Bearer ${key}`);
      });
    },
  };
}
