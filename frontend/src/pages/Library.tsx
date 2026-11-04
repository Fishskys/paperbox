import { App, Button, Drawer, Input, InputNumber, Select, Table, Tabs, Tag, Tooltip } from "antd";
import { ArrowUpRight, Download, Info, Pencil, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { errorMessage } from "../api";
import { Failure, PageHeader, Stage } from "../components";
import type { Paper, PaperList } from "../contracts";
import { downloadPaper, hasOriginal } from "../files";
import CitationActions from "../CitationActions";
import { useResource } from "../hooks";
import { positivePage, readerPath } from "../search-model";
import { remember } from "../history";

const statuses = [
  { label: "全部", key: "all" },
  { label: "待补原文", key: "AWAITING_FILE" },
  { label: "失败", key: "FAILED" },
  { label: "处理中", key: "PROCESSING" },
  { label: "待处理", key: "PENDING" },
  { label: "已存储", key: "STORED" },
  { label: "已索引", key: "INDEXED" },
];

export default function Library({
  title = "常规检索",
  onManage,
  revision = 0,
  conventional = false,
}: {
  title?: string;
  onManage?: (paper: Paper) => void;
  revision?: number;
  conventional?: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Paper | null>(null);
  const { message } = App.useApp();
  const location = useLocation();
  const page = positivePage(params.get("page"));
  const query = new URLSearchParams({
    limit: "20",
    offset: String((page - 1) * 20),
  });
  for (const key of ["q", "status", "author"])
    if (params.get(key)) query.set(key, params.get(key)!);
  query.set("scope", params.get("scope") || (conventional ? "all" : "title"));
  if (conventional) {
    query.set("facets", "true");
    for (const key of ["sort", "venue", "year_from", "year_to", "paper_type", "tag"])
      if (params.get(key)) query.set(key, params.get(key)!);
  }
  const { data, error, loading } = useResource<PaperList>(
    `/api/papers?${query}`,
    refresh + revision,
  );
  const encoded = params.toString();
  useEffect(() => {
    const p = new URLSearchParams(encoded);
    if (conventional && data && p.get("q")) remember("searches", {
      id: `standard:${encoded}`, query: p.get("q")!, search: encoded,
      path: "/search/standard", time: new Date().toISOString(),
    });
  }, [conventional, data, encoded]);
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("page");
    setParams(next);
  }
  async function download(paper: Paper) {
    try {
      await downloadPaper(paper.paper_id);
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }
  return (
    <>
      <PageHeader
        title={title}
        description={data ? `${data.total} 篇论文` : "研究资料"}
        actions={!conventional && <Button icon={<RefreshCw size={16} />} loading={loading} aria-label="刷新论文列表" onClick={() => setRefresh((value) => value + 1)}>刷新</Button>}
      />
      {!conventional && <Tabs className="management-status" activeKey={params.get("status") || "all"} items={statuses}
        onChange={(value) => filter("status", value === "all" ? "" : value)} />}
      <div className="list-toolbar">
        <Select aria-label={conventional ? "检索字段" : "定位字段"} value={params.get("scope") || (conventional ? "all" : "title")}
          options={(conventional ? [["all", "全部元数据"], ["title", "标题"], ["authors", "作者"], ["venue", "期刊 / 会议"], ["doi", "DOI"], ["arxiv_id", "arXiv ID"], ["abstract", "摘要"]] : [["title", "标题"], ["doi", "DOI"], ["authors", "作者"]]).map(([value, label]) => ({ value, label }))}
          onChange={(value) => filter("scope", value)} />
        <Input.Search
          aria-label={conventional ? "检索内容" : "快速定位论文"}
          key={`title-${params.get("q")}`}
          defaultValue={params.get("q") || ""}
          placeholder={conventional ? "输入检索内容" : "输入标题、DOI 或作者"}
          allowClear
          onSearch={(value) => filter("q", value)}
        />
        {conventional && <Select
          aria-label="处理状态"
          value={params.get("status") || ""}
          onChange={(value) => filter("status", value)}
          options={[
            { label: "全部状态", value: "" },
            { label: "待补原文（空壳论文）", value: "AWAITING_FILE" },
            { label: "待处理", value: "PENDING" },
            { label: "处理中", value: "PROCESSING" },
            { label: "已索引", value: "INDEXED" },
            { label: "失败", value: "FAILED" },
            { label: "已存储", value: "STORED" },
          ]}
        />}
      </div>
      {params.get("author") && <div className="active-paper-filters"><Tag closable onClose={() => filter("author", "")}>作者：{params.get("author")}</Tag></div>}
      {conventional && <div className="catalog-filters">
        <Input.Search className="catalog-tag" aria-label="筛选标签" placeholder="标签" defaultValue={params.get("tag") || ""}
          allowClear onSearch={(value) => filter("tag", value)} />
        <Select className="catalog-venue" aria-label="筛选期刊或会议" placeholder="期刊 / 会议" showSearch allowClear
          value={params.get("venue") || undefined}
          options={data?.facets?.venue?.map((item) => ({ value: item.key, label: `${item.key} (${item.count})` }))}
          onChange={(value) => filter("venue", value || "")} />
        <Select className="catalog-type" aria-label="文献类型" value={params.get("paper_type") || ""}
          options={[["", "全部类型"], ["journal", "期刊论文"], ["conference", "会议论文"], ["preprint", "预印本"], ["early_access", "在线优先"], ["standard", "标准"]].map(([value, label]) => ({ value, label }))}
          onChange={(value) => filter("paper_type", value)} />
        <div className="catalog-years">
        <InputNumber aria-label="起始年份" placeholder="起始年份" min={1900} max={2100} precision={0}
          value={params.get("year_from") ? Number(params.get("year_from")) : null} onChange={(value) => filter("year_from", value ? String(value) : "")} />
        <InputNumber aria-label="结束年份" placeholder="结束年份" min={1900} max={2100} precision={0}
          value={params.get("year_to") ? Number(params.get("year_to")) : null} onChange={(value) => filter("year_to", value ? String(value) : "")} />
        </div>
      </div>}
      {error && (
        <Failure message={error} retry={() => setRefresh((v) => v + 1)} />
      )}
      <Table<Paper>
        className={`data-table${conventional ? " catalog-table" : ""}`}
        rowKey="paper_id"
        loading={loading}
        dataSource={data?.papers || []}
        scroll={{ x: 720 }}
        locale={{ emptyText: error ? "论文列表加载失败" : "暂无论文" }}
        pagination={{
          placement: ["topEnd", "bottomEnd"],
          current: page,
          pageSize: 20,
          total: data?.total || 0,
          showSizeChanger: false,
          showQuickJumper: true,
          responsive: true,
          showLessItems: true,
          showTotal: conventional ? () => <span className="catalog-sort"><span>排序</span><Select aria-label="结果排序" value={params.get("sort") || "relevance"}
            options={[["relevance", "相关度"], ["newest", "最新入库"], ["year_desc", "年份从新到旧"], ["year_asc", "年份从旧到新"], ["title", "标题"]].map(([value, label]) => ({ value, label }))}
            onChange={(value) => filter("sort", value)} /></span> : undefined,
          onChange: (value) => {
            const next = new URLSearchParams(params);
            next.set("page", String(value));
            setParams(next);
          },
        }}
        columns={[
          {
            title: "论文",
            key: "paper",
            render: (_, paper) => (
              <div className="paper-cell">
                {!conventional ? <Button type="link" className="paper-title-button" onClick={() => onManage?.(paper)}>{paper.title || "未命名论文"}</Button> : hasOriginal(paper) ? (
                  <Link
                    to={readerPath(paper.paper_id)}
                    state={{ from: location.pathname + location.search }}
                  >
                    {paper.title} <ArrowUpRight size={13} />
                  </Link>
                ) : (
                  <Button
                    type="link"
                    className="paper-title-button"
                    onClick={() => setSelected(paper)}
                  >
                    {paper.title || "未命名论文"}
                  </Button>
                )}
                <p>{paper.authors.slice(0, 4).join(" · ") || "作者未标注"}</p>
                {!conventional && <p className="management-paper-details">{[paper.year, paper.venue, paper.doi].filter(Boolean).join(" · ")}</p>}
              </div>
            ),
          },
          {
            title: "年份",
            hidden: !conventional,
            dataIndex: "year",
            width: 75,
            render: (year) => year || "—",
          },
          {
            title: "期刊 / 会议",
            hidden: !conventional,
            dataIndex: "venue",
            width: 160,
            render: (venue) => venue || "—",
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 100,
            render: (value: string) => <Stage value={value} />,
          },
          {
            title: "原文",
            key: "original",
            hidden: conventional,
            width: 100,
            render: (_, paper) => <Tag color={hasOriginal(paper) ? "success" : "warning"}>{hasOriginal(paper) ? "有原文" : "待补充"}</Tag>,
          },
          {
            title: conventional ? "" : "操作",
            key: "actions",
            width: conventional ? 85 : 150,
            render: (_, paper) => !conventional ? <div className="inline">
              <Button type="text" icon={<Pencil size={16} />} aria-label={`管理论文 ${paper.title}`} onClick={() => onManage?.(paper)}>管理</Button>
              {hasOriginal(paper) && <Tooltip title="查看原文"><Link aria-label={`查看原文 ${paper.title}`} to={readerPath(paper.paper_id)} state={{ from: location.pathname + location.search }}><ArrowUpRight size={16} /></Link></Tooltip>}
            </div> : (
              <div className="inline">
                <Tooltip title="论文信息">
                  <Button
                    type="text"
                    aria-label={`论文信息 ${paper.title}`}
                    icon={<Info size={16} />}
                    onClick={() => setSelected(paper)}
                  />
                </Tooltip>
                <Tooltip title="下载原文">
                  <Button
                    type="text"
                    aria-label={`下载 ${paper.title}`}
                    disabled={!hasOriginal(paper)}
                    icon={<Download size={16} />}
                    onClick={() => void download(paper)}
                  />
                </Tooltip>
              </div>
            ),
          },
        ]}
      />
      <Drawer
        title="论文信息"
        open={!!selected}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="paper-info">
            <h2>{selected.title}</h2>
            <p>{selected.authors.join(", ")}</p>
            {!onManage && <CitationActions paperId={selected.paper_id} />}
            <Tag>{selected.year || "年份未知"}</Tag>
            <p>{selected.venue}</p>
            <h3>摘要</h3>
            <p>{selected.abstract || "暂无摘要"}</p>
            <h3>DOI</h3>
            <p>{selected.doi || "未标注"}</p>
            {hasOriginal(selected) ? (
              <Link
                to={readerPath(selected.paper_id)}
                state={{ from: location.pathname + location.search }}
              >
                打开论文
              </Link>
            ) : (
              <p className="muted">待补充 PDF 原文</p>
            )}
          </div>
        )}
      </Drawer>
    </>
  );
}
