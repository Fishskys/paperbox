import { Button, Skeleton, Table, Tag } from "antd";
import { ArrowUpRight, ScanLine } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Blank, Failure, formatTime, PageHeader } from "../components";
import type { Consistency } from "../contracts";
import { useResource } from "../hooks";
import { readerPath } from "../search-model";

export default function DataCheck() {
  const [run, setRun] = useState(0);
  const { data, error, loading } = useResource<Consistency>(
    run ? "/api/consistency?limit=100" : null,
    run,
  );
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
            onClick={() => setRun((v) => v + 1)}
          >
            开始检查
          </Button>
        }
      />
      {error && <Failure message={error} retry={() => setRun((v) => v + 1)} />}
      {loading && <Skeleton active />}
      {!run && <Blank text="尚未执行检查" />}
      {data && (
        <>
          <div className="section-heading">
            <span>检查于 {formatTime(data.checked_at)}</span>
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
                    <Link to={readerPath(paper.paper_id)}>
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
        </>
      )}
    </>
  );
}
