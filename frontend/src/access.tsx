import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Navigate, Outlet } from "react-router-dom";
import { errorMessage, request, useCredentialRevision } from "./api";
import { Failure } from "./components";
import type { AuthContext, Capability } from "./contracts";

type Access = {
  context?: AuthContext;
  loading: boolean;
  error?: string;
  can: (capability: Capability) => boolean;
  retry: () => void;
};
const Context = createContext<Access | null>(null);

export function AccessProvider({ children }: { children: ReactNode }) {
  const revision = useCredentialRevision();
  const [refresh, setRefresh] = useState(0);
  const version = `${revision}:${refresh}`;
  const [state, setState] = useState<{
    version: string;
    context?: AuthContext;
    error?: string;
  }>({ version: "" });
  useEffect(() => {
    const controller = new AbortController();
    request<AuthContext>("/api/auth/context", {
      signal: controller.signal,
      cache: "no-store",
    })
      .then((context) => {
        if (
          !context ||
          !["read", "write", "admin"].includes(context.role) ||
          !context.capabilities
        )
          throw new Error("后端未返回有效权限信息");
        if (!controller.signal.aborted) setState({ version, context });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({ version, error: errorMessage(error) });
      });
    return () => controller.abort();
  }, [version]);
  const current = state.version === version;
  const context = current ? state.context : undefined;
  const loading = !current;
  return (
    <Context.Provider
      value={{
        context,
        loading,
        error: current ? state.error : undefined,
        can: (capability) => context?.capabilities[capability] === true,
        retry: () => setRefresh((value) => value + 1),
      }}
    >
      {children}
    </Context.Provider>
  );
}

export function useAccess() {
  const value = useContext(Context);
  if (!value) throw new Error("AccessProvider is missing");
  return value;
}

export function RequireAccess({ capability }: { capability: Capability }) {
  const access = useAccess();
  if (access.loading) return <div className="blank">正在验证权限…</div>;
  if (access.error)
    return <Failure message={access.error} retry={access.retry} />;
  if (!access.can(capability)) return <Navigate to="/search" replace />;
  return <Outlet />;
}
