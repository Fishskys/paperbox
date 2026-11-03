import {
  App,
  Button,
  Descriptions,
  Modal,
  Select,
  Table,
  Tag,
} from "antd";
import { FileSearch, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, errorMessage, post } from "./api";
import { Failure } from "./components";
import type {
  MetadataConflict,
  MetadataReviewReport,
  ReviewSource,
} from "./contracts";
import { useResource } from "./hooks";
import SourceReviewDrawer from "./SourceReviewDrawer";
import SourceRematchBatch from "./SourceRematchBatch";

export default function MetadataReview({ active }: { active: boolean }) {
  const [refresh, setRefresh] = useState(0);
  const [status, setStatus] = useState("");
  const [sourcePage, setSourcePage] = useState(1);
  const [conflictPage, setConflictPage] = useState(1);
  const review = useResource<MetadataReviewReport>(
    active
      ? `/api/metadata/review?limit=20&offset=${(sourcePage - 1) * 20}&conflicts_offset=${(conflictPage - 1) * 20}${status ? `&status=${status}` : ""}`
      : null,
    refresh,
  );
  const [source, setSource] = useState<ReviewSource | null>(null);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [decision, setDecision] = useState<{
    conflict: MetadataConflict;
    adopt: boolean;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  const [error, setError] = useState("");
  const { message } = App.useApp();
  useEffect(() => {
    if (!review.data) return;
    setSourcePage((page) => Math.min(page, Math.max(1, Math.ceil(review.data!.total / 20))));
    setConflictPage((page) => Math.min(page, Math.max(1, Math.ceil((review.data!.conflicts_total ?? review.data!.conflicts.length) / 20))));
  }, [review.data]);
  useEffect(() => {
    if (!active) {
      setSource(null);
      setDecision(null);
    }
  }, [active]);
  async function submit() {
    if (guard.current || !decision) return;
    guard.current = true;
    setBusy(true);
    setError("");
    try {
      if (decision) {
        const { conflict, adopt } = decision;
        if (!conflict.provenance_id) throw new Error("缺少可复核的字段记录");
        await post(
          `/api/papers/${encodeURIComponent(conflict.paper_id)}/metadata/${adopt ? "rollback" : "conflicts/dismiss"}`,
          { field: conflict.field, provenance_id: conflict.provenance_id },
        );
      }
      setDecision(null);
      setSource(null);
      setRefresh((value) => value + 1);
      void message.success("复核已保存");
    } catch (error) {
      setError(
        !(error instanceof ApiError) || error.status >= 500
          ? `${errorMessage(error)}。请刷新复核列表核对结果后再操作。`
          : errorMessage(error),
      );
    } finally {
      guard.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <div className="list-toolbar">
        <Select
          aria-label="来源匹配状态"
          value={status}
          onChange={(value) => { setStatus(value); setSourcePage(1); setSelected([]); }}
          options={[
            { value: "", label: "全部待复核来源" },
            { value: "ambiguous", label: "匹配有歧义" },
            { value: "pending", label: "待匹配" },
            { value: "rejected", label: "已拒绝" },
          ]}
        />
        <Button
          icon={<RefreshCw size={16} />}
          loading={review.loading}
          disabled={busy}
          onClick={() => {
            setError("");
            setRefresh((value) => value + 1);
          }}
        >
          刷新复核列表
        </Button>
        <SourceRematchBatch sourceIds={selected.map(String)} onDone={() => { setSelected([]); setRefresh((n) => n + 1); }} />
        {!!selected.length && <span className="muted">已选择 {selected.length} 条</span>}
      </div>
      {review.error && (
        <Failure
          message={review.error}
          retry={() => setRefresh((value) => value + 1)}
        />
      )}
      {error && !source && !decision && <Failure message={error} />}
      {review.data && (
        <p className="muted">
          共 {review.data.total} 条来源、
          {review.data.conflicts_total ?? review.data.conflicts.length} 项字段冲突。
        </p>
      )}
      <h2>{status === "rejected" ? "已拒绝来源" : "待复核来源"}</h2>
      <Table<ReviewSource>
        className="data-table"
        rowKey="source_id"
        loading={review.loading}
        dataSource={review.data?.items || []}
        rowSelection={status === "rejected" ? undefined : { selectedRowKeys: selected, onChange: setSelected, preserveSelectedRowKeys: true }}
        scroll={{ x: 650 }}
        pagination={{ current: sourcePage, pageSize: 20, total: review.data?.total, showSizeChanger: false, showQuickJumper: true, onChange: setSourcePage }}
        locale={{ emptyText: review.error ? "来源加载失败" : "暂无待复核来源" }}
        columns={[
          {
            title: "来源记录",
            dataIndex: "source_ref",
            render: (value) => (
              <span className="metadata-reference">{value}</span>
            ),
          },
          { title: "类型", dataIndex: "source_type", width: 110 },
          {
            title: "状态",
            dataIndex: "match_status",
            width: 110,
            render: (value) => (
              <Tag>
                {value === "ambiguous"
                  ? "匹配有歧义"
                  : value === "pending"
                    ? "待匹配"
                    : value === "rejected"
                    ? "已拒绝"
                    : value}
              </Tag>
            ),
          },
          { title: "匹配方式", dataIndex: "match_method", width: 120 },
          {
            title: "",
            width: 120,
            render: (_, item) => (
              <Button
                icon={<FileSearch size={14} />}
                disabled={busy}
                onClick={() => {
                  setSource(item);
                  setError("");
                }}
              >
                查看与处理
              </Button>
            ),
          },
        ]}
      />
      <h2>字段冲突</h2>
      <Table<MetadataConflict>
        className="data-table"
        rowKey={(conflict) =>
          `${conflict.paper_id}:${conflict.field}:${conflict.provenance_id}`
        }
        loading={review.loading}
        dataSource={review.data?.conflicts || []}
        scroll={{ x: 800 }}
        pagination={{ current: conflictPage, pageSize: 20, total: review.data?.conflicts_total ?? review.data?.conflicts.length, showSizeChanger: false, showQuickJumper: true, onChange: setConflictPage }}
        locale={{ emptyText: review.error ? "冲突加载失败" : "暂无待复核冲突" }}
        columns={[
          {
            title: "论文",
            width: 100,
            render: (_, item) => (
              <Link
                to={`/manage/library?paper=${encodeURIComponent(item.paper_id)}`}
              >
                查看论文
              </Link>
            ),
          },
          { title: "字段", dataIndex: "field", width: 110 },
          {
            title: "当前值",
            dataIndex: "kept",
            render: (value) => (
              <span className="metadata-detail">{JSON.stringify(value)}</span>
            ),
          },
          {
            title: "被拒值",
            dataIndex: "rejected",
            render: (value) => (
              <span className="metadata-detail">{JSON.stringify(value)}</span>
            ),
          },
          {
            title: "操作",
            width: 250,
            render: (_, conflict) => (
              <div className="inline">
                <Button
                  disabled={busy || !conflict.provenance_id}
                  onClick={() => {
                    setError("");
                    setDecision({ conflict, adopt: false });
                  }}
                >
                  保留现值
                </Button>
                <Button
                  disabled={busy || !conflict.provenance_id}
                  onClick={() => {
                    setError("");
                    setDecision({ conflict, adopt: true });
                  }}
                >
                  采纳被拒值
                </Button>
              </div>
            ),
          },
        ]}
      />
      {source && <SourceReviewDrawer key={source.source_id} sourceId={source.source_id}
        onClose={() => setSource(null)} onDone={() => { setSelected([]); setRefresh((n) => n + 1); }} />}
      <Modal
        title={decision?.adopt ? "采纳被拒值" : "保留现值"}
        open={!!decision}
        onCancel={() => {
          if (!busy) setDecision(null);
        }}
        onOk={() => void submit()}
        okText="确认裁决"
        cancelText="取消"
        confirmLoading={busy}
        cancelButtonProps={{ disabled: busy }}
      >
        {error && <Failure message={error} />}
        {decision && (
          <Descriptions
            column={1}
            items={[
              {
                key: "field",
                label: "字段",
                children: decision.conflict.field,
              },
              {
                key: "kept",
                label: "当前值",
                children: (
                  <span className="metadata-detail">
                    {JSON.stringify(decision.conflict.kept)}
                  </span>
                ),
              },
              {
                key: "rejected",
                label: "被拒值",
                children: (
                  <span className="metadata-detail">
                    {JSON.stringify(decision.conflict.rejected)}
                  </span>
                ),
              },
            ]}
          />
        )}
      </Modal>
    </>
  );
}
