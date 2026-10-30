import {
  Button,
  Collapse,
  Input,
  InputNumber,
  Pagination,
  Select,
  Skeleton,
  Switch,
  Tag,
} from "antd";
import {
  ArrowUpRight,
  FileText,
  Search as SearchIcon,
  SlidersHorizontal,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { errorMessage, post, useCredentialRevision } from "../api";
import { Blank, Failure, PageHeader } from "../components";
import type { SearchResponse } from "../contracts";
import { remember } from "../history";
import { DEFAULT_RECALL_K, DEFAULT_TOP_K, readerPath, searchBody } from "../search-model";

const scrollPositions = new Map<string, number>();

export default function Search() {
  const [params, setParams] = useSearchParams();
  const encoded = params.toString();
  const revision = useCredentialRevision();
  const [query, setQuery] = useState(params.get("q") || "");
  const [mode, setMode] = useState(params.get("mode") || "hybrid");
  const [author, setAuthor] = useState(params.get("author") || "");
  const [venue, setVenue] = useState(params.get("venue") || "");
  const [yearFrom, setYearFrom] = useState<number | null>(
    params.get("year_from") ? Number(params.get("year_from")) : null,
  );
  const [yearTo, setYearTo] = useState<number | null>(
    params.get("year_to") ? Number(params.get("year_to")) : null,
  );
  const [rerank, setRerank] = useState(params.get("rerank") === "true");
  const [rewrite, setRewrite] = useState(params.get("rewrite") === "true");
  const [excludeReferences, setExcludeReferences] = useState(params.get("include_references") !== "true");
  const [recallK, setRecallK] = useState(Number(params.get("recall_k") || DEFAULT_RECALL_K));
  const [topK, setTopK] = useState(Number(params.get("top_k") || DEFAULT_TOP_K));
  const [paperType, setPaperType] = useState(params.get("paper_type") || "");
  const [tag, setTag] = useState(params.get("tag") || "");
  const [state, setState] = useState<{
    data?: SearchResponse;
    error?: string;
    loading: boolean;
  }>({ loading: false });
  const [refresh, setRefresh] = useState(0);
  const location = useLocation();

  useEffect(() => {
    const p = new URLSearchParams(encoded);
    setQuery(p.get("q") || "");
    setMode(p.get("mode") || "hybrid");
    setAuthor(p.get("author") || "");
    setVenue(p.get("venue") || "");
    setYearFrom(p.get("year_from") ? Number(p.get("year_from")) : null);
    setYearTo(p.get("year_to") ? Number(p.get("year_to")) : null);
    setRerank(p.get("rerank") === "true");
    setRewrite(p.get("rewrite") === "true");
    setExcludeReferences(p.get("include_references") !== "true");
    setRecallK(Number(p.get("recall_k") || DEFAULT_RECALL_K));
    setTopK(Number(p.get("top_k") || DEFAULT_TOP_K));
    setPaperType(p.get("paper_type") || "");
    setTag(p.get("tag") || "");
    if (!p.get("q")) {
      setState({ loading: false });
      return;
    }
    const controller = new AbortController();
    setState({ loading: true });
    try {
      const body = searchBody(p);
      post<SearchResponse>("/api/search", body, controller.signal, 180000)
        .then((data) => {
          if (controller.signal.aborted) return;
          setState({ data, loading: false });
          remember("searches", {
            id: encoded,
            query: body.query,
            search: encoded,
            time: new Date().toISOString(),
          });
          requestAnimationFrame(() =>
            window.scrollTo(0, scrollPositions.get(encoded) || 0),
          );
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted)
            setState({ error: errorMessage(error), loading: false });
        });
    } catch (error) {
      setState({ error: errorMessage(error), loading: false });
    }
    return () => controller.abort();
  }, [encoded, revision, refresh]);

  function submit() {
    const next = new URLSearchParams({ q: query.trim(), mode });
    if (author.trim()) next.set("author", author.trim());
    if (venue.trim()) next.set("venue", venue.trim());
    if (yearFrom !== null) next.set("year_from", String(yearFrom));
    if (yearTo !== null) next.set("year_to", String(yearTo));
    if (rerank) next.set("rerank", "true");
    next.set("recall_k", String(recallK));
    next.set("top_k", String(topK));
    if (rewrite) next.set("rewrite", "true");
    if (!excludeReferences) next.set("include_references", "true");
    if (paperType) next.set("paper_type", paperType);
    if (tag.trim()) next.set("tag", tag.trim());
    try {
      searchBody(next);
    } catch (error) {
      setState({ error: errorMessage(error), loading: false });
      return;
    }
    scrollPositions.delete(next.toString());
    if (next.toString() === encoded) setRefresh((v) => v + 1);
    else setParams(next);
  }
  function saveScroll() {
    scrollPositions.set(encoded, window.scrollY);
  }
  const from = { from: location.pathname + location.search };
  return (
    <>
      <PageHeader
        title="智能检索"
        description="发现研究，回到证据"
        actions={
          <Link className="quiet-link" to="/search/history">
            检索记录 <ArrowUpRight size={15} />
          </Link>
        }
      />
      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="search-row">
          <Input
            aria-label="查询内容"
            size="large"
            prefix={<SearchIcon size={18} />}
            placeholder="搜索标题、主题或研究问题"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            allowClear
          />
          <Button
            type="primary"
            size="large"
            htmlType="submit"
            loading={state.loading}
          >
            搜索论文
          </Button>
        </div>
        <div className="search-options">
          <Select
            aria-label="检索模式"
            value={mode}
            onChange={setMode}
            options={[
              { label: "混合检索", value: "hybrid" },
              { label: "关键词", value: "keyword" },
              { label: "语义检索", value: "semantic" },
            ]}
          />
          <span className="inline">精排
          <Switch
            aria-label="开启精排"
            size="small"
            checked={rerank}
            onChange={setRerank}
          />
          </span>
          <span className="inline">查询改写 <Switch aria-label="查询改写" checked={rewrite} onChange={setRewrite} size="small" /></span>
          <span className="inline">排除参考文献 <Switch aria-label="排除参考文献" checked={excludeReferences} onChange={setExcludeReferences} size="small" /></span>
          <label>召回论文数 <InputNumber aria-label="召回论文数" min={1} max={500} value={recallK} onChange={(value) => setRecallK(value || DEFAULT_RECALL_K)} /></label>
          <label>每页结果数 <InputNumber aria-label="每页结果数" min={1} max={50} value={topK} onChange={(value) => setTopK(value || DEFAULT_TOP_K)} /></label>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: "filters",
              label: (
                <span className="inline">
                  <SlidersHorizontal size={14} /> 筛选条件
                </span>
              ),
              children: (
                <div className="filter-grid">
                  <label>文献类型<Select aria-label="智能检索文献类型" value={paperType} onChange={setPaperType}
                    options={[["", "全部类型"], ["journal", "期刊论文"], ["conference", "会议论文"], ["preprint", "预印本"], ["early_access", "在线优先"], ["standard", "标准"]].map(([value, label]) => ({value, label}))} /></label>
                  <label>标签<Input value={tag} onChange={(event) => setTag(event.target.value)} /></label>
                  <label>
                    作者
                    <Input
                      value={author}
                      onChange={(e) => setAuthor(e.target.value)}
                      placeholder="作者姓名"
                    />
                  </label>
                  <label>
                    期刊 / 会议
                    <Input
                      value={venue}
                      onChange={(e) => setVenue(e.target.value)}
                      placeholder="期刊或会议名称"
                    />
                  </label>
                  <label>
                    起始年份
                    <InputNumber
                      min={1900}
                      max={2100}
                      value={yearFrom}
                      onChange={setYearFrom}
                    />
                  </label>
                  <label>
                    结束年份
                    <InputNumber
                      min={1900}
                      max={2100}
                      value={yearTo}
                      onChange={setYearTo}
                    />
                  </label>
                </div>
              ),
            },
          ]}
        />
      </form>
      {state.error && (
        <Failure message={state.error} retry={() => setRefresh((v) => v + 1)} />
      )}
      {state.loading && (
        <div className="results">
          <Skeleton active paragraph={{ rows: 5 }} />
          <Skeleton active paragraph={{ rows: 5 }} />
        </div>
      )}
      {state.data && (
        <>
          <div className="result-summary">
            <span>
              {state.data.total_scope === "candidate_window" ? "候选窗口" : "找到"} <strong>{state.data.total}</strong> 篇论文 · 显示{" "}
              {state.data.results.length} 篇
            </span>
            <span>{Math.round(state.data.took_ms)} ms</span>
          </div>
          <div className="data-summary">
            <span>执行模式：{state.data.mode || mode} / {state.data.backend || "未知"}</span>
            <span>召回论文：{state.data.recalled_papers ?? "未知"}</span>
            <span>重排：{({ not_requested: "未请求", disabled: "服务未启用", applied: "已执行", failed: "失败，使用召回排序", empty: "无候选结果" } as Record<string, string>)[state.data.rerank?.status || ""] || "状态未知"}</span>
            <span>改写：{state.data.rewrite?.applied ? "已执行" : "未执行"}</span>
          </div>
          <Pagination current={Number(params.get("page") || 1)} pageSize={topK}
            total={state.data.recalled_papers ?? state.data.results.length} showSizeChanger={false}
            onChange={(page) => { const next = new URLSearchParams(params); next.set("page", String(page)); setParams(next); }} />
          <div className="results">
            {state.data.results.map((paper) => (
              <article className="search-result" key={paper.paper_id}>
                <div className="result-title">
                  <FileText size={19} />
                  <Link
                    to={readerPath(paper.paper_id)}
                    state={from}
                    onClick={saveScroll}
                  >
                    {paper.title}
                  </Link>
                  <span className="result-year">{paper.year || "—"}</span>
                </div>
                <p className="paper-byline">
                  {paper.authors.join(" · ") || "作者未标注"}
                  {paper.venue && (
                    <>
                      {" "}
                      <span className="separator">/</span> {paper.venue}
                    </>
                  )}
                </p>
                {paper.evidence[0] && (
                  <p className="evidence-preview">
                    {paper.evidence[0].text.slice(0, 260)}
                    {paper.evidence[0].text.length > 260 ? "…" : ""}
                  </p>
                )}
                <div className="result-footer">
                  <span>{paper.venue && <Tag>{paper.venue}</Tag>}</span>
                  <Link
                    to={readerPath(paper.paper_id)}
                    state={from}
                    onClick={saveScroll}
                  >
                    阅读论文 <ArrowUpRight size={14} />
                  </Link>
                </div>
                {!!paper.evidence.length && (
                  <Collapse
                    ghost
                    items={[
                      {
                        key: "evidence",
                        label: `${paper.evidence.length} 条命中证据`,
                        children: paper.evidence.map((evidence) => (
                          <div className="evidence" key={evidence.chunk_id}>
                            <div>
                              <span className="muted">
                                {evidence.section || "正文片段"}
                              </span>
                              <Link
                                to={readerPath(paper.paper_id, evidence.page)}
                                state={from}
                                onClick={saveScroll}
                              >
                                {evidence.page
                                  ? `第 ${evidence.page} 页`
                                  : "打开原文"}{" "}
                                <ArrowUpRight size={13} />
                              </Link>
                            </div>
                            <p>{evidence.text}</p>
                          </div>
                        )),
                      },
                    ]}
                  />
                )}
              </article>
            ))}
          </div>
          {!state.data.results.length && <Blank text="没有匹配的论文" />}
        </>
      )}
      {!state.data && !state.loading && !state.error && (
        <Blank text="开始一次新的检索" />
      )}
    </>
  );
}
