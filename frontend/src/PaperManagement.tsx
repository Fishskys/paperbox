import {
  Alert,
  App,
  Button,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Table,
  Tabs,
  Tag,
  Upload,
} from "antd";
import { Eraser, RefreshCw, Save, ScanLine, Trash2, Upload as UploadIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post, request } from "./api";
import { Failure, formatTime } from "./components";
import type {
  Accepted,
  Paper,
  PaperMetadata,
  ReextractReport,
} from "./contracts";
import { hasOriginal } from "./files";
import { useResource } from "./hooks";
import { metadataChanges, metadataValues } from "./metadata-form";

type Action =
  | { kind: "delete" | "reindex" }
  | {
      kind: "clear";
      preview: {
        source_rows: number;
        provenance_rows: number;
        identifier_rows: number;
      };
    }
  | { kind: "reextract"; preview: ReextractReport };
const actionLabels = {
  delete: "删除论文",
  reindex: "重建索引",
  clear: "清除元数据",
  reextract: "重新提取元数据",
};
const textFields = [
  ["venue", "期刊 / 会议"],
  ["doi", "DOI"],
  ["arxiv_id", "arXiv ID"],
  ["volume", "卷号"],
  ["issue", "期号"],
  ["pages", "页码范围"],
  ["url", "来源链接"],
] as const;

export default function PaperManagement({
  paperId,
  onChanged,
  onDeleted,
}: {
  paperId: string;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [revision, setRevision] = useState(0);
  const base = `/api/papers/${encodeURIComponent(paperId)}`;
  const paper = useResource<Paper>(base, revision);
  const metadata = useResource<PaperMetadata>(`${base}/metadata`, revision);
  const [form] = Form.useForm();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  const [error, setError] = useState("");
  const [action, setAction] = useState<Action | null>(null);
  const [original, setOriginal] = useState<File | null>(null);
  const [attachmentJob, setAttachmentJob] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{
    text: string;
    job?: string;
    warning?: boolean;
  } | null>(null);
  useEffect(() => {
    if (paper.data) form.setFieldsValue(metadataValues(paper.data));
  }, [paper.data, form]);
  async function attachOriginal() {
    if (!original || guard.current) return;
    guard.current = true;
    setBusy(true);
    setError("");
    setOutcome(null);
    try {
      const body = new FormData();
      body.append("file", original);
      const result = await request<Accepted>(`${base}/file`, { method: "POST", body });
      setAttachmentJob(result.job_id);
      setOutcome({ text: "原文补传任务已接收，解析与索引进度请查看任务。", job: result.job_id });
      setRevision((value) => value + 1);
      onChanged();
    } catch (error) {
      setError(`${errorMessage(error)}；提交结果不明确时，请先查看该论文的任务。`);
    } finally {
      setOriginal(null);
      guard.current = false;
      setBusy(false);
    }
  }
  async function save(values: Partial<Paper>) {
    if (!paper.data || guard.current) return;
    const changes = metadataChanges(paper.data, values);
    if (!Object.keys(changes).length) {
      void message.info("没有需要保存的修改");
      return;
    }
    guard.current = true;
    setBusy(true);
    setError("");
    setOutcome(null);
    try {
      const result = await request<{ rejected: string[] }>(`${base}/metadata`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      setOutcome({
        text: result.rejected.length
          ? `部分字段未保存：${result.rejected.join("、")}`
          : "元数据已保存；检索中的信息需在重建索引后更新。",
        warning: !!result.rejected.length,
      });
      setRevision((value) => value + 1);
      onChanged();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      guard.current = false;
      setBusy(false);
    }
  }
  async function prepare(kind: Action["kind"]) {
    if (guard.current) return;
    setError("");
    setOutcome(null);
    if (kind === "delete" || kind === "reindex") {
      setAction({ kind });
      return;
    }
    guard.current = true;
    setBusy(true);
    try {
      if (kind === "clear")
        setAction({
          kind,
          preview: await request<{
            source_rows: number;
            provenance_rows: number;
            identifier_rows: number;
          }>(`${base}/metadata?dry_run=true`, { method: "DELETE" }),
        });
      else
        setAction({
          kind,
          preview: await post<ReextractReport>(`${base}/metadata/reextract`, {
            dry_run: true,
            refresh_index: true,
            detail: true,
          }),
        });
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      guard.current = false;
      setBusy(false);
    }
  }
  async function perform() {
    if (!action || guard.current) return;
    guard.current = true;
    setBusy(true);
    setError("");
    try {
      if (action.kind === "delete") {
        await request(base, { method: "DELETE" });
        onDeleted();
        return;
      }
      if (action.kind === "reindex") {
        const accepted = await post<Accepted>(`${base}/reindex`, {});
        setOutcome({ text: "重建任务已接收", job: accepted.job_id });
      } else if (action.kind === "clear") {
        await request(`${base}/metadata?dry_run=false`, { method: "DELETE" });
        setOutcome({
          text: "元数据已清除，PDF 原文与正文保留；检索中的旧信息需在重建索引后更新。",
        });
      } else {
        const result = await post<ReextractReport>(
          `${base}/metadata/reextract`,
          { dry_run: false, refresh_index: true, detail: true },
        );
        setOutcome({
          text: `已更新 ${result.changed_papers} 篇，失败 ${result.failed} 篇`,
          warning: result.failed > 0,
        });
      }
      setAction(null);
      setRevision((value) => value + 1);
      onChanged();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      guard.current = false;
      setBusy(false);
    }
  }
  if (paper.loading) return <Skeleton active />;
  if (paper.error)
    return (
      <Failure
        message={paper.error}
        retry={() => setRevision((value) => value + 1)}
      />
    );
  if (!paper.data) return null;
  const document = paper.data;
  return (
    <>
      <h2>{document.title || "未命名论文"}</h2>
      <p className="muted">{document.paper_id}</p>
      <Link to={`/imports/jobs?paper_id=${encodeURIComponent(document.paper_id)}`}>查看相关任务</Link>
      {!hasOriginal(document) && (
        <Alert type="info" showIcon title="待补充 PDF 原文" action={
          <Upload
            accept=".pdf,application/pdf"
            showUploadList={false}
            disabled={busy || !!attachmentJob}
            beforeUpload={(file) => {
              setError("");
              setOriginal(file);
              return false;
            }}
          >
            <Button icon={<UploadIcon size={16} />} disabled={busy || !!attachmentJob}>
              补传原文
            </Button>
          </Upload>
        } />
      )}
      <Modal
        title="补传原文"
        open={!!original}
        okText="确认上传"
        cancelText="取消"
        confirmLoading={busy}
        closable={!busy}
        mask={{ closable: !busy }}
        cancelButtonProps={{ disabled: busy }}
        onCancel={() => !busy && setOriginal(null)}
        onOk={() => void attachOriginal()}
      >
        <p>论文：{document.title}</p>
        <p>文件：{original?.name}</p>
        <p>请确认 PDF 属于这篇论文。文件将绑定此论文，并创建解析和索引任务。</p>
      </Modal>
      {error && !action && <Failure message={error} />}
      {outcome && (
        <Alert
          type={outcome.warning ? "warning" : "success"}
          showIcon
          title={outcome.text}
          description={
            outcome.job && (
              <Link to={`/imports/jobs?job=${encodeURIComponent(outcome.job)}`}>
                查看任务
              </Link>
            )
          }
        />
      )}
      <Tabs
        items={[
          {
            key: "edit",
            label: "元数据",
            children: (
              <Form
                form={form}
                layout="vertical"
                onFinish={(values) => void save(values)}
                disabled={busy}
              >
                <Form.Item
                  name="title"
                  label="标题"
                  rules={[
                    {
                      required: true,
                      whitespace: true,
                      message: "请输入论文标题",
                    },
                  ]}
                >
                  <Input />
                </Form.Item>
                <Form.Item name="authors" label="作者">
                  <Select
                    mode="tags"
                    tokenSeparators={[",", ";"]}
                    options={document.authors.map((author) => ({
                      label: author,
                      value: author,
                    }))}
                  />
                </Form.Item>
                <div className="metadata-form-grid">
                  <Form.Item name="year" label="年份">
                    <InputNumber min={1900} max={2100} precision={0} />
                  </Form.Item>
                  <Form.Item name="paper_type" label="文献类型">
                    <Select
                      allowClear
                      options={[
                        { value: "journal", label: "期刊" },
                        { value: "conference", label: "会议" },
                        { value: "preprint", label: "预印本" },
                        { value: "early_access", label: "提前发表" },
                        { value: "standard", label: "标准" },
                      ]}
                    />
                  </Form.Item>
                  {textFields.map(([field, label]) => (
                    <Form.Item key={field} name={field} label={label}>
                      <Input />
                    </Form.Item>
                  ))}
                  <Form.Item name="publication_date" label="发表日期">
                    <Input type="date" />
                  </Form.Item>
                </div>
                <Form.Item name="abstract" label="摘要">
                  <Input.TextArea rows={5} />
                </Form.Item>
                <Button
                  htmlType="submit"
                  type="primary"
                  icon={<Save size={16} />}
                  loading={busy}
                >
                  保存元数据
                </Button>
              </Form>
            ),
          },
          {
            key: "history",
            label: "来源与历史",
            children: (
              <>
                {metadata.error && <Failure message={metadata.error} />}
                <h3>来源记录</h3>
                <Table
                  className="data-table"
                  loading={metadata.loading}
                  rowKey="source_id"
                  dataSource={metadata.data?.sources || []}
                  scroll={{ x: 450 }}
                  columns={[
                    { title: "类型", dataIndex: "source_type" },
                    { title: "来源", dataIndex: "source_ref" },
                  ]}
                />
                <h3>字段历史</h3>
                <Table
                  className="data-table"
                  loading={metadata.loading}
                  rowKey="provenance_id"
                  dataSource={Object.entries(
                    metadata.data?.provenance || {},
                  ).flatMap(([field, entries]) =>
                    entries.map((entry) => ({ field, ...entry })),
                  )}
                  scroll={{ x: 650 }}
                  columns={[
                    { title: "字段", dataIndex: "field" },
                    {
                      title: "值",
                      dataIndex: "value",
                      render: (value) => (
                        <span className="metadata-detail">
                          {JSON.stringify(value)}
                        </span>
                      ),
                    },
                    {
                      title: "状态",
                      dataIndex: "is_current",
                      render: (current) => (
                        <Tag>{current ? "当前" : "历史"}</Tag>
                      ),
                    },
                    { title: "决定者", dataIndex: "decided_by" },
                    {
                      title: "时间",
                      dataIndex: "decided_at",
                      render: formatTime,
                    },
                  ]}
                />
              </>
            ),
          },
          {
            key: "maintenance",
            label: "操作",
            children: (
              <div className="metadata-actions">
                <Button
                  icon={<RefreshCw size={16} />}
                  disabled={busy || !hasOriginal(document)}
                  onClick={() => void prepare("reindex")}
                >
                  重建索引
                </Button>
                <Button
                  icon={<ScanLine size={16} />}
                  disabled={busy || !hasOriginal(document)}
                  onClick={() => void prepare("reextract")}
                >
                  重新提取元数据
                </Button>
                <Button
                  icon={<Eraser size={16} />}
                  disabled={busy}
                  onClick={() => void prepare("clear")}
                >
                  清除元数据
                </Button>
                <Button
                  danger
                  icon={<Trash2 size={16} />}
                  disabled={busy}
                  onClick={() => void prepare("delete")}
                >
                  删除论文
                </Button>
              </div>
            ),
          },
        ]}
      />
      <Modal
        title={action ? actionLabels[action.kind] : "确认操作"}
        open={!!action}
        onCancel={() => {
          if (!busy) {
            setAction(null);
            setError("");
          }
        }}
        onOk={() => void perform()}
        okText="确认执行"
        cancelText="取消"
        confirmLoading={busy}
        cancelButtonProps={{ disabled: busy }}
        okButtonProps={{
          danger: action?.kind === "delete" || action?.kind === "clear",
          disabled:
            action?.kind === "reextract" &&
            (action.preview.failed > 0 || action.preview.changed_papers === 0),
        }}
      >
        {error && <Failure message={error} />}
        {action?.kind === "delete" && (
          <p>删除论文：{document.title || document.paper_id}</p>
        )}
        {action?.kind === "reindex" && (
          <p>将重新解析原文、切块并计算向量，处理完成后更新检索索引。</p>
        )}
        {action?.kind === "clear" && (
          <>
            <Alert
              type="warning"
              showIcon
              title="将清除元数据、来源记录和字段历史，保留 PDF 原文与正文。"
            />
            <Descriptions
              column={1}
              items={[
                {
                  key: "sources",
                  label: "来源记录",
                  children: action.preview.source_rows,
                },
                {
                  key: "history",
                  label: "字段历史",
                  children: action.preview.provenance_rows,
                },
                {
                  key: "ids",
                  label: "标识符",
                  children: action.preview.identifier_rows,
                },
              ]}
            />
          </>
        )}
        {action?.kind === "reextract" && (
          <>
            <p>
              预览：涉及 {action.preview.changed_papers} 篇，失败{" "}
              {action.preview.failed} 篇；只补充缺失信息。
            </p>
            <Table
              size="small"
              rowKey="field"
              pagination={false}
              dataSource={action.preview.changes}
              columns={[
                { title: "字段", dataIndex: "field" },
                {
                  title: "新值",
                  dataIndex: "new",
                  render: (value) => JSON.stringify(value),
                },
              ]}
            />
            {action.preview.errors.map((item, index) => (
              <Failure key={index} message={item.error} />
            ))}
          </>
        )}
      </Modal>
    </>
  );
}
