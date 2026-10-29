import {
  Button,
  Collapse,
  Input,
  InputNumber,
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
import { readerPath, searchBody } from "../search-model";

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
    if (!p.get("q")) {
      setState({ loading: false });
      return;
    }
    const controller = new AbortController();
    setState({ loading: true });
    try {
      const body = searchBody(p);
      post<SearchResponse>("/api/search", body, controller.signal)
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
        title="论文搜索"
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
          <span className="muted">精排</span>
          <Switch
            aria-label="开启精排"
            size="small"
            checked={rerank}
            onChange={setRerank}
          />
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
              找到 <strong>{state.data.total}</strong> 篇论文 · 显示{" "}
              {state.data.results.length} 篇
            </span>
            <span>{Math.round(state.data.took_ms)} ms</span>
          </div>
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
