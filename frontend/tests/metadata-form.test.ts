import { describe, expect, it } from "vitest";
import type { Paper } from "../src/contracts";
import { metadataChanges, metadataValues } from "../src/metadata-form";
import { hasOriginal } from "../src/files";

const paper: Paper = {
  paper_id: "one",
  title: "Clock design",
  authors: ["Chen"],
  doi: "10.1234/example",
  status: "INDEXED",
};

describe("metadata edits", () => {
  it("does not send unchanged or absent fields", () => {
    expect(metadataChanges(paper, metadataValues(paper))).toEqual({});
    expect(metadataChanges(paper, { ...paper, title: "New title" })).toEqual({
      title: "New title",
    });
  });
  it("preserves explicit clearing of identifiers and authors", () => {
    expect(metadataChanges(paper, { ...paper, doi: "", authors: [] })).toEqual({
      doi: null,
      authors: [],
    });
  });
  it("distinguishes a metadata-only paper from an available PDF", () => {
    expect(hasOriginal({ ...paper, files: [] })).toBe(false);
    expect(
      hasOriginal({ ...paper, files: [{ mime_type: "application/pdf" }] }),
    ).toBe(true);
    expect(hasOriginal({ ...paper, status: "AWAITING_FILE" })).toBe(false);
  });
});
