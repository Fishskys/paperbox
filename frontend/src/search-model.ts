export const modes = ["hybrid", "keyword", "semantic"] as const;
export function searchBody(params: URLSearchParams) {
  const query = params.get("q")?.trim() || "";
  const mode = params.get("mode") || "hybrid";
  if (!query) throw new Error("请输入查询内容");
  if (!modes.includes(mode as (typeof modes)[number]))
    throw new Error("检索模式无效");
  const filters: Record<string, string | number | string[]> = {};
  for (const key of ["year_from", "year_to"]) {
    const raw = params.get(key);
    if (!raw) continue;
    const year = Number(raw);
    if (!Number.isInteger(year) || year < 1900 || year > 2100)
      throw new Error("年份应为 1900–2100 之间的整数");
    filters[key] = year;
  }
  if (Number(filters.year_from) > Number(filters.year_to))
    throw new Error("起始年份不能大于结束年份");
  if (params.get("author")?.trim())
    filters.authors = [params.get("author")!.trim()];
  if (params.get("venue")?.trim())
    filters.venue = [params.get("venue")!.trim()];
  for (const key of ["paper_type", "tag", "doi", "arxiv_id"])
    if (params.get(key)?.trim()) filters[key] = params.get(key)!.trim();
  const topK = Number(params.get("top_k") || 20);
  const recallK = Number(params.get("recall_k") || 100);
  const offset = (positivePage(params.get("page")) - 1) * topK;
  if (!Number.isInteger(topK) || topK < 1 || topK > 50 || !Number.isInteger(recallK) || recallK < topK || recallK > 500 || offset >= recallK)
    throw new Error("结果数应为 1–50，召回数应覆盖当前页且不超过 500");
  return {
    query,
    mode,
    top_k: topK,
    recall_k: recallK,
    offset,
    backend: "native",
    rewrite: params.get("rewrite") === "true",
    rerank: params.get("rerank") === "true",
    filters,
  };
}

export function positivePage(
  value: string | null,
  max = Number.MAX_SAFE_INTEGER,
) {
  const page = Number(value);
  return Number.isSafeInteger(page) && page >= 1 ? Math.min(page, max) : 1;
}

export function readerPath(id: string, page?: number | null) {
  return `/reading/papers/${encodeURIComponent(id)}${page && Number.isInteger(page) && page > 0 ? `?page=${page}` : ""}`;
}
