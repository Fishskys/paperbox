import type { ProcessMemory } from "./observability-contracts";

export type Dependency = {
  reason?: string;
  latency_ms?: number;
  checked_at?: string;
  cached?: boolean;
  required?: boolean;
};
type Service = { name: string; status: string; detail?: Dependency };
export type DependencyGroup = {
  key: string;
  title: string;
  status: string;
  services: Service[];
  memory?: ProcessMemory;
};
const names: Record<string, string> = {
  postgres: "PostgreSQL", opensearch: "OpenSearch", minio: "MinIO",
  embedding: "Embedding", rerank: "Reranker", docling: "Docling",
};
export const serviceNames: Record<string, string> = { embedding: "向量", rerank: "重排" };

export function groupDependencies(
  services: Record<string, string>,
  details: Record<string, Dependency> = {},
  measurements: Record<string, ProcessMemory> = {},
): DependencyGroup[] {
  const groups = new Map<string, DependencyGroup>();
  for (const [name, status] of Object.entries(services)) {
    const memory = measurements[name];
    const key = memory?.source === "container" && memory.container
      ? `container:${memory.container}` : `service:${name}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, title: "", status: "unknown", memory, services: [] };
      groups.set(key, group);
    }
    group.services.push({ name, status, detail: details[name] });
  }
  for (const group of groups.values()) {
    const states = group.services.map(service => service.status);
    group.status = states.includes("error") ? "error"
      : states.some(state => !["ok", "disabled"].includes(state)) ? "unknown"
      : states.every(state => state === "disabled") ? "disabled"
      : states.includes("disabled") ? "partial" : "ok";
    const modelOnly = group.services.every(service => ["embedding", "rerank"].includes(service.name));
    group.title = modelOnly && group.services.some(service => service.name === "embedding")
      ? "Embedding" : group.services.map(service => names[service.name] || service.name).join(" / ");
  }
  return [...groups.values()];
}
