import { describe, expect, it } from "vitest";
import type { ProcessMemory } from "../src/observability-contracts";
import { groupDependencies } from "../src/system-status";

const memory = (container: string): ProcessMemory => ({ rss_bytes: 100, pss_bytes: 80, container,
  processes: 1, partial: false, source: "container", reason: null });

describe("container status cards", () => {
  it("groups by actual container identity, including custom names, without adding shared memory twice", () => {
    const measured = memory("custom-model-service");
    const groups = groupDependencies({ embedding: "ok", rerank: "ok", docling: "ok" }, {}, {
      embedding: measured, rerank: measured, docling: memory("custom-docling"),
    });
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ title: "Embedding", status: "ok", memory: { rss_bytes: 100 } });
    expect(groups[0].services.map(item => item.name)).toEqual(["embedding", "rerank"]);
    expect(groups[1].title).toBe("Docling");
  });
  it("preserves individual health and does not hide rerank failure behind a healthy embedding service", () => {
    const measured = memory("shared");
    const [group] = groupDependencies({ embedding: "ok", rerank: "error" }, {
      rerank: { reason: "rerank unavailable", required: false },
    }, { embedding: measured, rerank: measured });
    expect(group.status).toBe("error");
    expect(group.services[1].detail?.reason).toBe("rerank unavailable");
    expect(groupDependencies({ embedding: "ok", rerank: "disabled" }, {}, { embedding: measured, rerank: measured })[0].status).toBe("partial");
  });
  it("keeps separate or unknown containers separate rather than guessing from service names", () => {
    expect(groupDependencies({ embedding: "ok", rerank: "ok" })).toHaveLength(2);
    expect(groupDependencies({ embedding: "ok", rerank: "ok" }, {}, {
      embedding: memory("first"), rerank: memory("second"),
    })).toHaveLength(2);
    expect(groupDependencies({ embedding: "disabled", rerank: "disabled" }, {}, {
      embedding: memory("same"), rerank: memory("same"),
    })[0].status).toBe("disabled");
  });
});
