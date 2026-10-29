import { describe, expect, it } from "vitest";
import { positivePage, readerPath, searchBody } from "../src/search-model";

describe("search and reading contracts", () => {
  it("emits supported filter types and a real boolean for rerank", () => {
    expect(
      searchBody(
        new URLSearchParams(
          "q=clock&author=Chen&venue=ISSCC&year_from=2020&rerank=true",
        ),
      ),
    ).toEqual({
      query: "clock",
      mode: "hybrid",
      top_k: 20,
      rerank: true,
      filters: { authors: ["Chen"], venue: ["ISSCC"], year_from: 2020 },
    });
  });
  it("does not silently submit invalid year ranges or modes", () => {
    expect(() =>
      searchBody(new URLSearchParams("q=x&year_from=2025&year_to=2020")),
    ).toThrow();
    expect(() => searchBody(new URLSearchParams("q=x&mode=broken"))).toThrow();
    expect(() =>
      searchBody(new URLSearchParams("q=x&year_from=abc")),
    ).toThrow();
  });
  it("does not fabricate evidence pages and clamps invalid deep links", () => {
    expect(readerPath("abc", null)).toBe("/reading/papers/abc");
    expect(readerPath("abc", 3)).toBe("/reading/papers/abc?page=3");
    expect(positivePage("999", 8)).toBe(8);
    expect(positivePage("-2")).toBe(1);
    expect(positivePage("NaN")).toBe(1);
  });
});
