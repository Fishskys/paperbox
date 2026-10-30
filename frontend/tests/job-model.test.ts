import { describe, expect, it } from "vitest";
import { jobDuration, jobKind } from "../src/job-model";

describe("job diagnostics", () => {
  it("does not infer a task type from its stage", () => {
    expect(jobKind()).toBe("未知");
    expect(jobKind("figures")).toBe("图片提取");
    expect(jobKind("future-kind")).toBe("future-kind");
  });
  it("reports observed time including queueing and prefers the finish timestamp", () => {
    const job = { job_id: "one", stage: "FAILED", progress: 30,
      created_at: "2026-10-30T10:00:00Z", updated_at: "2026-10-30T10:03:00Z" };
    expect(jobDuration(job)).toBe("3 分 0 秒");
    expect(jobDuration({ ...job, finished_at: "2026-10-30T10:01:05Z" })).toBe("1 分 5 秒");
    expect(jobDuration({ ...job, created_at: null })).toBe("未知");
    expect(jobDuration({ ...job, created_at: "bad" })).toBe("未知");
  });
});
