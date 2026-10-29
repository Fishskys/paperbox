import {
  createContext,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";
import type { ReactNode } from "react";
import { UploadQueue } from "./upload-queue";

const Context = createContext<UploadQueue | null>(null);
export function UploadProvider({ children }: { children: ReactNode }) {
  const [queue] = useState(() => new UploadQueue());
  useEffect(() => () => queue.stopQueued(), [queue]);
  const items = useSyncExternalStore(queue.subscribe, queue.snapshot);
  const pending = items.some((item) =>
    ["uploading", "backoff"].includes(item.state),
  );
  useEffect(() => {
    if (!pending) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [pending]);
  return <Context.Provider value={queue}>{children}</Context.Provider>;
}
export function useUploads() {
  const queue = useContext(Context);
  if (!queue) throw new Error("UploadProvider is missing");
  const items = useSyncExternalStore(queue.subscribe, queue.snapshot);
  return { queue, items };
}
