import type { Paper } from "./contracts";

const textFields = [
  "venue",
  "doi",
  "arxiv_id",
  "abstract",
  "paper_type",
  "volume",
  "issue",
  "pages",
  "publication_date",
  "url",
] as const;

export function metadataValues(paper: Partial<Paper>): Record<string, unknown> {
  return {
    title: paper.title?.trim() || "",
    authors: (paper.authors || [])
      .map((author) => author.trim())
      .filter(Boolean),
    year: paper.year ?? null,
    ...Object.fromEntries(
      textFields.map((field) => [field, paper[field]?.trim() || null]),
    ),
  };
}

export function metadataChanges(original: Paper, edited: Partial<Paper>) {
  const before = metadataValues(original);
  return Object.fromEntries(
    Object.entries(metadataValues(edited)).filter(
      ([field, value]) =>
        JSON.stringify(value) !== JSON.stringify(before[field]),
    ),
  );
}
