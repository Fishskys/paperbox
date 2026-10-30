import { useEffect, useState } from "react";
import { errorMessage, request, useCredentialRevision } from "./api";

export function useResource<T>(path: string | null, refresh = 0, timeoutMs = 30000) {
  const revision = useCredentialRevision();
  const [state, setState] = useState<{
    data?: T;
    error?: string;
    loading: boolean;
  }>({ loading: !!path });
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    setState({ loading: true });
    request<T>(path, { signal: controller.signal }, timeoutMs)
      .then((data) => {
        if (!controller.signal.aborted) setState({ data, loading: false });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({ error: errorMessage(error), loading: false });
      });
    return () => controller.abort();
  }, [path, refresh, revision, timeoutMs]);
  return state;
}

export function useVisible() {
  const [visible, setVisible] = useState(
    document.visibilityState === "visible",
  );
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}
