import { App, Button, Drawer, Input, Select, Table, Tag, Tooltip } from "antd";
import { ArrowUpRight, Download, Info } from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { errorMessage } from "../api";
import { Failure, PageHeader, Stage } from "../components";
import type { Paper, PaperList } from "../contracts";
import { downloadPaper } from "../files";
import { useResource } from "../hooks";
import { positivePage, readerPath } from "../search-model";

export default function Library() {
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
  const { data, error, loading } = useResource<PaperList>(
    `/api/papers?${query}`,
    refresh,
  );
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
        title="论文库"
        description={data ? `${data.total} 篇论文` : "研究资料"}
      />
      <div className="list-toolbar">
        <Input.Search
          aria-label="搜索论文标题"
          key={`title-${params.get("q")}`}
          defaultValue={params.get("q") || ""}
          placeholder="搜索论文标题"
          allowClear
          onSearch={(value) => filter("q", value)}
        />
        <Input.Search
          aria-label="筛选作者"
          key={`author-${params.get("author")}`}
          defaultValue={params.get("author") || ""}
          placeholder="作者"
          allowClear
          onSearch={(value) => filter("author", value)}
        />
        <Select
          aria-label="处理状态"
          value={params.get("status") || ""}
          onChange={(value) => filter("status", value)}
          options={[
            { label: "全部状态", value: "" },
            { label: "已索引", value: "INDEXED" },
            { label: "失败", value: "FAILED" },
            { label: "已存储", value: "STORED" },
          ]}
        />
      </div>
      {error && (
        <Failure message={error} retry={() => setRefresh((v) => v + 1)} />
      )}
      <Table<Paper>
        className="data-table"
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
                <Link
                  to={readerPath(paper.paper_id)}
                  state={{ from: location.pathname + location.search }}
                >
                  {paper.title} <ArrowUpRight size={13} />
                </Link>
                <p>{paper.authors.slice(0, 4).join(" · ") || "作者未标注"}</p>
              </div>
            ),
          },
          {
            title: "年份",
            dataIndex: "year",
            width: 75,
            render: (year) => year || "—",
          },
          {
            title: "期刊 / 会议",
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
            title: "",
            key: "actions",
            width: 85,
            render: (_, paper) => (
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
            <Tag>{selected.year || "年份未知"}</Tag>
            <p>{selected.venue}</p>
            <h3>摘要</h3>
            <p>{selected.abstract || "暂无摘要"}</p>
            <h3>DOI</h3>
            <p>{selected.doi || "未标注"}</p>
            <Link
              to={readerPath(selected.paper_id)}
              state={{ from: location.pathname + location.search }}
            >
              打开论文
            </Link>
          </div>
        )}
      </Drawer>
    </>
  );
}
