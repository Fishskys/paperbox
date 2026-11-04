import { Alert, App, Button, Collapse, Drawer, Input, Progress, Select, Table, Tooltip } from "antd";
import { ArrowUpRight, RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { errorMessage, post, request, useCredentialRevision } from "../api";
import { Failure, formatTime, PageHeader, Stage } from "../components";
import type { Job, JobList, QueueSnapshot } from "../contracts";
import { useResource, useVisible } from "../hooks";
import { positivePage, readerPath } from "../search-model";
import { jobDuration, jobKind } from "../job-model";

const stages = [
  "RECEIVED",
  "QUEUED",
  "DOWNLOADING",
  "STORED",
  "PARSING",
  "CHUNKING",
  "EMBEDDING",
  "INDEXING",
  "COMPLETED",
  "FAILED",
];
const active = (job: Job) => !["COMPLETED", "FAILED"].includes(job.stage);

export default function Jobs() {
  const [params, setParams] = useSearchParams();
  const encoded = params.toString();
  const page = positivePage(params.get("page"));
  const visible = useVisible();
  const revision = useCredentialRevision();
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{
    data?: JobList;
    error?: string;
    loading: boolean;
    checked?: string;
    query?: string;
  }>({ loading: true });
  const [retrying, setRetrying] = useState<string | null>(null);
  const { message } = App.useApp();
  const detail = useResource<Job>(
    params.get("job")
      ? `/api/jobs/${encodeURIComponent(params.get("job")!)}`
      : null,
    refresh,
  );
  const queue = useResource<QueueSnapshot>(visible ? "/api/jobs/queue" : null, refresh);
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({
      limit: "20",
      offset: String(
        (positivePage(new URLSearchParams(encoded).get("page")) - 1) * 20,
      ),
    });
    const stage = new URLSearchParams(encoded).get("stage");
    if (stage) query.set("stage", stage);
    const paperId = new URLSearchParams(encoded).get("paper_id");
    if (paperId) query.set("paper_id", paperId);
    setState((previous) =>
      previous.query === encoded
        ? { ...previous, loading: true, error: undefined }
        : { query: encoded, loading: true },
    );
    async function load() {
      try {
        const data = await request<JobList>(`/api/jobs?${query}`, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setState({
          data,
          query: encoded,
          loading: false,
          checked: new Date().toISOString(),
        });
      } catch (error) {
        if (!controller.signal.aborted)
          setState((previous) => ({
            ...previous,
            loading: false,
            error: errorMessage(error),
          }));
      }
    }
    if (visible) void load();
    else setState((previous) => ({ ...previous, loading: false }));
    return () => controller.abort();
  }, [encoded, refresh, visible, revision]);
  useEffect(() => {
    if (
      !visible ||
      state.error ||
      !(state.data?.jobs.some(active) ||
        (params.get("job") && detail.data && active(detail.data)) ||
        (!queue.error && queue.data && (queue.data.running > 0 || queue.data.queued > 0)))
    )
      return;
    const timer = setTimeout(() => setRefresh((v) => v + 1), 3000);
    return () => clearTimeout(timer);
  }, [visible, state.data, state.error, detail.data, params, queue.data, queue.error]);
  async function retry(job: Job) {
    setRetrying(job.job_id);
    try {
      await post(`/api/jobs/${encodeURIComponent(job.job_id)}/retry`, {});
      setRefresh((v) => v + 1);
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      setRetrying(null);
    }
  }
  function show(job: Job) {
    const next = new URLSearchParams(params);
    next.set("job", job.job_id);
    setParams(next);
  }
  return (
    <>
      <PageHeader
        title="处理任务"
        description={
          state.checked ? `更新于 ${formatTime(state.checked)}` : "处理队列"
        }
        actions={
          <Button
            icon={<RefreshCw size={16} />}
            loading={state.loading}
            onClick={() => setRefresh((v) => v + 1)}
          >
            刷新
          </Button>
        }
      />
      <div className="list-toolbar">
        <Input.Search
          aria-label="按论文 ID 筛选任务"
          placeholder="论文 ID"
          key={params.get("paper_id") || "all-papers"}
          defaultValue={params.get("paper_id") || ""}
          allowClear
          onSearch={(value) => {
            const next = new URLSearchParams(params);
            if (value.trim()) next.set("paper_id", value.trim());
            else next.delete("paper_id");
            next.delete("page");
            setParams(next);
          }}
        />
        <Select
          aria-label="任务阶段"
          value={params.get("stage") || ""}
          options={[
            { label: "全部阶段", value: "" },
            ...stages.map((stage) => ({
              label: <Stage value={stage} />,
              value: stage,
            })),
          ]}
          onChange={(value) => {
            const next = new URLSearchParams(params);
            if (value) next.set("stage", value);
            else next.delete("stage");
            next.delete("page");
            setParams(next);
          }}
        />
      </div>
      {queue.error && <Failure message={`队列状态：${queue.error}`} />}
      {queue.data && !queue.error && (
        <div className="data-summary" aria-label="任务队列状态">
          <span>运行中 <strong>{queue.data.running}</strong></span>
          <span>排队中 <strong>{queue.data.queued}</strong></span>
          <span>交互队列 <strong>{queue.data.queued_high}</strong></span>
          <span>批量队列 <strong>{queue.data.queued_low}</strong></span>
          <span>并发上限 <strong>{queue.data.concurrency}</strong></span>
        </div>
      )}
      {queue.data && !queue.data.started && !queue.error && <Alert type="warning" showIcon title="任务队列尚未启动" />}
      {state.error && (
        <Failure message={state.error} retry={() => setRefresh((v) => v + 1)} />
      )}
      <Table<Job>
        className="data-table"
        rowKey="job_id"
        dataSource={state.data?.jobs || []}
        loading={state.loading && !state.data}
        scroll={{ x: 750 }}
        pagination={{
          placement: ["topEnd", "bottomEnd"],
          current: page,
          pageSize: 20,
          total: state.data?.total || 0,
          showSizeChanger: false,
          showQuickJumper: true,
          onChange: (value) => {
            const next = new URLSearchParams(params);
            next.set("page", String(value));
            setParams(next);
          },
        }}
        locale={{ emptyText: state.error ? "任务列表加载失败" : "暂无任务" }}
        columns={[
          {
            title: "任务",
            key: "job",
            render: (_, job) => (
              <div className="paper-cell">
                <Button
                  type="link"
                  className="job-id"
                  onClick={() => show(job)}
                >
                  {job.job_id.slice(0, 12)}
                </Button>
                <p>
                  {job.error_code && <code>{job.error_code}: </code>}
                  {job.error_message ||
                    (job.paper_id
                      ? `论文 ${job.paper_id.slice(0, 8)}`
                      : "等待关联论文")}
                </p>
              </div>
            ),
          },
          { title: "类型", key: "kind", width: 110, render: (_, job) => jobKind(job.kind) },
          {
            title: "阶段",
            dataIndex: "stage",
            width: 115,
            render: (value: string) => <Stage value={value} />,
          },
          {
            title: "进度",
            key: "progress",
            width: 140,
            render: (_, job) => (
              <Progress
                size="small"
                percent={Math.max(0, Math.min(100, job.progress))}
                status={
                  job.stage === "FAILED"
                    ? "exception"
                    : job.stage === "COMPLETED"
                      ? "success"
                      : "normal"
                }
              />
            ),
          },
          {
            title: "更新时间",
            dataIndex: "updated_at",
            width: 180,
            render: (value: string) => formatTime(value),
          },
          {
            title: "",
            key: "actions",
            width: 95,
            render: (_, job) => (
              <div className="inline">
                {job.stage === "FAILED" && job.kind !== "maintenance" && (
                  <Tooltip title="重试任务">
                    <Button
                      type="text"
                      aria-label={`重试任务 ${job.job_id}`}
                      loading={retrying === job.job_id}
                      disabled={retrying !== null}
                      icon={<RotateCcw size={16} />}
                      onClick={() => void retry(job)}
                    />
                  </Tooltip>
                )}
                {job.paper_id && job.stage === "COMPLETED" && (
                  <Tooltip title="打开论文">
                    <Link aria-label="打开论文" to={readerPath(job.paper_id)}>
                      <ArrowUpRight size={18} />
                    </Link>
                  </Tooltip>
                )}
              </div>
            ),
          },
        ]}
      />
      <Drawer
        title="任务详情"
        open={!!params.get("job")}
        onClose={() => {
          const next = new URLSearchParams(params);
          next.delete("job");
          setParams(next);
        }}
      >
        {detail.error && <Failure message={detail.error} />}
        {detail.data && (
          <div className="paper-info">
            <h3>任务 ID</h3>
            <p>{detail.data.job_id}</p>
            <h3>类型</h3>
            <p>{jobKind(detail.data.kind)}</p>
            <Stage value={detail.data.stage} />
            <Progress
              percent={Math.max(0, Math.min(100, detail.data.progress))}
            />
            <h3>更新时间</h3>
            <p>{formatTime(detail.data.updated_at)}</p>
            <h3>创建时间</h3>
            <p>{formatTime(detail.data.created_at)}</p>
            <h3>完成时间</h3>
            <p>{formatTime(detail.data.finished_at)}</p>
            <h3>总耗时（含排队）</h3>
            <p>{jobDuration(detail.data)}</p>
            {!!detail.data.deduplication_audit?.length && <>
              <h3>判重与文件处置</h3>
              <Collapse items={detail.data.deduplication_audit.map(event => ({
                key: event.event_id,
                label: `${({ byte_duplicate: "字节重复", identity_match: "身份匹配", version_selection: "版本关联", fingerprint_conflict: "指纹冲突", temporary_cleanup: "临时文件清理" } as Record<string, string>)[event.kind] || event.kind} · ${event.rule}`,
                children: <>
                  <p>{formatTime(event.created_at)} · {event.actor}</p>
                  <Link to={`/manage/library?paper=${encodeURIComponent(event.target_paper_id)}`}>目标论文 {event.target_paper_id}</Link>
                  {event.request_id && <p>请求 {event.request_id}</p>}
                  <h4>比较证据</h4><pre className="report-value">{JSON.stringify(event.evidence, null, 2)}</pre>
                  <h4>文件处置</h4><pre className="report-value">{JSON.stringify(event.file_action, null, 2)}</pre>
                </>,
              }))} />
            </>}
            {detail.data.duplicate && !detail.data.deduplication_audit?.length && <p>此历史任务未记录详细判重依据。</p>}
            {detail.data.error_code && <p>错误码：<code>{detail.data.error_code}</code></p>}
            {detail.data.error_message && (
              <Failure message={detail.data.error_message} />
            )}
            {detail.data.paper_id && (
              <div className="metadata-actions">
                <Link to={`/manage/library?paper=${encodeURIComponent(detail.data.paper_id)}`}>管理论文</Link>
                {detail.data.stage === "COMPLETED" && <Link to={readerPath(detail.data.paper_id)}>打开论文</Link>}
              </div>
            )}
          </div>
        )}
      </Drawer>
    </>
  );
}
