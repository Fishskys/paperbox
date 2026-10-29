import { useMemo, useSyncExternalStore } from "react";

let credential = "";
let revision = 0;
let connection: "unknown" | "online" | "offline" = "unknown";
const listeners = new Set<() => void>();
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
function notify() {
  listeners.forEach((listener) => listener());
}
export function useConnection() {
  return useSyncExternalStore(subscribe, () => connection);
}
export function useCredentialRevision() {
  return useSyncExternalStore(subscribe, () => revision);
}
export function setCredential(value: string) {
  credential = value.trim();
  revision++;
  notify();
}
export function authHeaders(): Record<string, string> {
  return credential ? { Authorization: `Bearer ${credential}` } : {};
}
export function useAuthHeaders() {
  const key = useSyncExternalStore(subscribe, () => credential);
  return useMemo(() => {
    const headers: Record<string, string> = {};
    if (key) headers.Authorization = `Bearer ${key}`;
    return headers;
  }, [key]);
}
export function markConnection(value: typeof connection) {
  connection = value;
  notify();
}

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string,
    public retryAfter: string | null = null,
  ) {
    super(
      status === 401
        ? `需要 API 凭证。${detail}`
        : status === 403
          ? `访问被拒绝。${detail}`
          : detail,
    );
    this.name = "ApiError";
  }
}

export function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "请求失败，请重试";
}

export function detailMessage(payload: unknown): string {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail)
      return String(detail.message);
    return JSON.stringify(detail);
  }
  return "服务未能完成请求";
}

export function checkResponse(
  status: number,
  payload: unknown,
  retryAfter: string | null = null,
) {
  const gatewayFailure = status >= 500 && payload === null;
  markConnection(gatewayFailure ? "offline" : "online");
  if (status < 200 || status >= 300) {
    throw new ApiError(
      status,
      gatewayFailure
        ? "后端不可达或网关异常，请检查 API 服务"
        : detailMessage(payload),
      retryAfter,
    );
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  try {
    const response = await fetch(path, {
      ...options,
      signal: options.signal
        ? AbortSignal.any([options.signal, AbortSignal.timeout(30000)])
        : AbortSignal.timeout(30000),
      headers: {
        Accept: "application/json",
        ...authHeaders(),
        ...options.headers,
      },
    });
    const payload: unknown = await response.json().catch(() => null);
    checkResponse(
      response.status,
      payload,
      response.headers.get("Retry-After"),
    );
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError || options.signal?.aborted) throw error;
    markConnection("offline");
    throw new Error("无法连接 Paperbox，请检查服务后重试", { cause: error });
  }
}

export function post<T>(path: string, body: unknown, signal?: AbortSignal) {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}
