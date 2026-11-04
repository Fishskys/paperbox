import { Button, Skeleton, Table, Tag } from "antd";
import { ArrowUpRight, Download, ScanLine } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Blank, Failure, formatTime, PageHeader } from "../components";
import type { Consistency, IdentifierCheck } from "../contracts";
import { useResource } from "../hooks";
import { downloadJson } from "../files";
import { MaintenanceTaskStatus, useMaintenanceTask } from "../maintenance-tasks";

export default function DataCheck() {
  const [identifierRun, setIdentifierRun] = useState(0);
  const identifiers = useResource<IdentifierCheck>(identifierRun ? "/api/consistency/doi?limit=100" : null, identifierRun, 180000);
  const task = useMaintenanceTask<Consistency>("consistency");
  const data = task.report;
  const loading = task.active;
  const incomplete = !!data?.errors.length;
  return (
    <>
      <PageHeader
        title="数据检查"
        description="论文、原件与检索索引"
        actions={
          <Button
            type="primary"
            icon={<ScanLine size={16} />}
            loading={loading}
            onClick={() => void task.submit({ operation: "consistency", limit: 100 })}
          >
            开始检查
          </Button>
        }
      />
      <MaintenanceTaskStatus task={task} />
      {loading && <Skeleton active />}
      {!task.job && !task.error && <Blank text="尚未执行检查" />}
      {data && (
        <>
          <div className="section-heading">
            <span>检查于 {formatTime(data.checked_at)}</span>
            <Button icon={<Download size={16} />} onClick={() => downloadJson(data, `paperbox-consistency-${Date.now()}.json`)}>下载报告</Button>
            <Tag
              color={
                incomplete ? "warning" : data.consistent ? "success" : "error"
              }
            >
              {incomplete
                ? "结果不完整"
                : data.consistent
                  ? "数据一致"
                  : "发现差异"}
            </Tag>
          </div>
          {data.errors.map((message, i) => (
            <Failure key={i} message={message} />
          ))}
          <div className="data-summary">
            <span>
              论文 <strong>{data.totals.papers_live}</strong>
            </span>
            <span>
              存储对象 <strong>{data.totals.objects_minio}</strong>
            </span>
            <span>
              索引片段 <strong>{data.totals.documents_os}</strong>
            </span>
            <span>
              问题论文 <strong>{data.totals.problems}</strong>
            </span>
          </div>
          <Table
            className="data-table"
            rowKey="paper_id"
            dataSource={data.problems}
            scroll={{ x: 600 }}
            pagination={false}
            expandable={{ expandedRowRender: (paper) => <div className="report-value">
              <p>状态：{paper.status || "未知"}；原文记录 / 存储对象：{paper.files_pg ?? "—"} / {paper.objects_minio ?? "—"}；片段记录 / 索引：{paper.chunks_pg ?? "—"} / {paper.chunks_os ?? "—"}</p>
              <p>解析器：{paper.parser_backend || "未知"}；向量模型：{paper.embedding_models?.join("、") || "未知"}</p>
              {!!paper.missing_objects?.length && <p>缺失对象：{paper.missing_objects.join("、")}</p>}
              {!!paper.orphan_objects?.length && <p>多余对象：{paper.orphan_objects.join("、")}</p>}
            </div> }}
            locale={{
              emptyText: incomplete
                ? "检查未完成，暂未获得问题列表"
                : "未发现问题论文",
            }}
            columns={[
              {
                title: "论文",
                key: "paper",
                render: (_, paper: Consistency["problems"][number]) =>
                  paper.deleted ? (
                    <span>
                      {paper.title || paper.paper_id} <Tag>已删除</Tag>
                    </span>
                  ) : (
                    <Link to={`/manage/library?paper=${encodeURIComponent(paper.paper_id)}`}>
                      {paper.title || paper.paper_id}
                      <ArrowUpRight size={14} />
                    </Link>
                  ),
              },
              {
                title: "差异",
                key: "issues",
                render: (_, paper: Consistency["problems"][number]) =>
                  paper.issues.map((issue) => <Tag key={issue}>{issue}</Tag>),
              },
            ]}
          />
          {data.truncated && (
            <p className="muted">列表仅展示前 100 项，以报告汇总为准。</p>
          )}
          <div className="section-heading">解析器与向量模型分布</div>
          <Table className="data-table" rowKey="name" pagination={false} dataSource={[
            ...Object.entries(data.parser_backends?.papers || {}).map(([name, count]) => ({ name: `解析器：${name}`, count, indexed: data.parser_backends?.documents[name] || 0 })),
            ...Object.entries(data.embedding_models?.chunks || {}).map(([name, count]) => ({ name: `向量模型：${name}`, count, indexed: data.embedding_models?.documents[name] || 0 })),
          ]} columns={[{ title: "类型", dataIndex: "name" }, { title: "数据库", dataIndex: "count" }, { title: "索引文档", dataIndex: "indexed" }]} />
          <div className="section-heading">孤立对象 {data.totals.orphan_objects || 0} · 孤立索引文档 {data.totals.orphan_documents || 0}</div>
          <Table className="data-table" rowKey="_key" pagination={{ pageSize: 20 }} dataSource={[
            ...(data.orphan_objects || []).map((name) => ({ name, type: "存储对象" })), ...(data.orphan_documents || []).map((name) => ({ name, type: "索引文档" })),
          ].map((row, i) => ({ ...row, _key: String(i) }))} columns={[{ title: "类型", dataIndex: "type" }, { title: "标识", dataIndex: "name", render: (name) => <span className="report-value">{name}</span> }]} />
        </>
      )}
      <div className="section-heading"><strong>DOI / arXiv 标识一致性</strong><Button icon={<ScanLine size={16} />} loading={identifiers.loading} onClick={() => setIdentifierRun((value) => value + 1)}>检查标识</Button></div>
      {identifiers.error && <Failure message={identifiers.error} />}
      {identifiers.data && <>
        <div className="metadata-actions"><Tag color={identifiers.data.errors.length ? "warning" : identifiers.data.consistent ? "success" : "error"}>{identifiers.data.errors.length ? "结果不完整" : identifiers.data.consistent ? "标识一致" : "标识存在差异"}</Tag>
          <Button icon={<Download size={16} />} onClick={() => downloadJson(identifiers.data, `paperbox-identifiers-${Date.now()}.json`)}>下载标识报告</Button></div>
        {identifiers.data.errors.map((message, i) => <Failure key={i} message={message} />)}
        <Table className="data-table" rowKey="_key" dataSource={identifiers.data.drifts.map((row, i) => ({ ...row, _key: String(i) }))} scroll={{ x: 650 }} columns={[
          { title: "论文", render: (_, row) => <Link to={`/manage/library?paper=${encodeURIComponent(row.paper_id)}`}>{row.paper_id}</Link> },
          { title: "标识", dataIndex: "scheme" }, { title: "差异", dataIndex: "kind" }, { title: "镜像值", render: (_, row) => String(row.mirror_value ?? "—") },
          { title: "登记值", render: (_, row) => row.identifier_values.join("、") }, { title: "说明", dataIndex: "detail" },
        ]} />
        {identifiers.data.truncated && <p className="muted">仅显示前 100 项差异。</p>}
      </>}
    </>
  );
}
