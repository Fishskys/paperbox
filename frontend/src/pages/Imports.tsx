import {
  App,
  Button,
  Input,
  Progress,
  Segmented,
  Tag,
  Tooltip,
  Upload,
} from "antd";
import {
  ArrowUpRight,
  Archive,
  FolderOpen,
  FileText,
  Link as LinkIcon,
  Pause,
  Play,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post } from "../api";
import { Blank, PageHeader } from "../components";
import type { Accepted } from "../contracts";
import { readerPath } from "../search-model";
import { useUploads } from "../upload-context";
import ServerImports from "../ServerImports";

const labels = {
  waiting: "等待上传",
  uploading: "上传中",
  backoff: "等待重试",
  accepted: "已接收",
  duplicate: "已在库中",
  failed: "失败",
  stopped: "已停止",
};

export default function Imports() {
  const { queue, items } = useUploads();
  const [mode, setMode] = useState("files");
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [accepted, setAccepted] = useState<Accepted | null>(null);
  const guard = useRef(false);
  const { message } = App.useApp();
  const waiting = items.some((item) => item.state === "waiting");
  const active = items.some((item) =>
    ["uploading", "backoff"].includes(item.state),
  );
  async function submitUrl() {
    if (guard.current) return;
    try {
      const parsed = new URL(url);
      if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
    } catch {
      void message.error("请输入有效的 HTTP(S) PDF 链接");
      return;
    }
    guard.current = true;
    setSubmitting(true);
    setAccepted(null);
    try {
      setAccepted(
        await post<Accepted>("/api/papers/ingest", {
          source_type: "url",
          source: url.trim(),
        }),
      );
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      guard.current = false;
      setSubmitting(false);
    }
  }
  return (
    <>
      <PageHeader
        title="论文导入"
        description="将研究资料加入论文库"
        actions={
          <Link className="quiet-link" to="/imports/jobs">
            导入任务 <ArrowUpRight size={15} />
          </Link>
        }
      />
      <Segmented
        aria-label="导入方式"
        value={mode}
        onChange={setMode}
        options={[
          {
            label: (
              <span className="inline">
                <UploadCloud size={15} /> PDF
              </span>
            ),
            value: "files",
          },
          {
            label: (
              <span className="inline">
                <LinkIcon size={15} /> 链接导入
              </span>
            ),
            value: "url",
          },
          { value: "archive", label: <span className="inline"><Archive size={15} /> ZIP</span> },
          { value: "directory", label: <span className="inline"><FolderOpen size={15} /> 目录</span> },
        ]}
      />
      {mode === "archive" || mode === "directory" ? <ServerImports key={mode} mode={mode} /> : mode === "files" ? (
        <div className="import-workspace">
          <Upload.Dragger
            accept=".pdf,application/pdf"
            multiple
            showUploadList={false}
            beforeUpload={(_, files) => {
              if (files[0] === _) queue.add(files);
              return false;
            }}
          >
            <UploadCloud size={34} className="upload-mark" />
            <p className="upload-title">拖放 PDF 文件到这里</p>
            <p className="muted">或点击选择文件</p>
          </Upload.Dragger>
          <div className="upload-toolbar">
            <span className="muted">{items.length} 个文件</span>
            <div className="inline">
              <Tooltip title="清理已结束项">
                <Button
                  aria-label="清理已结束项"
                  icon={<Trash2 size={16} />}
                  onClick={() => queue.clearFinished()}
                />
              </Tooltip>
              <Button
                icon={<Pause size={15} />}
                disabled={!waiting && !active}
                onClick={() => queue.stopQueued()}
              >
                停止待传
              </Button>
              <Button
                type="primary"
                icon={<Play size={15} />}
                disabled={!waiting}
                onClick={() => queue.start()}
              >
                开始上传
              </Button>
            </div>
          </div>
          {!items.length && <Blank text="暂无待传文件" />}
          {items.map((item) => (
            <div className="upload-row" key={item.id}>
              <FileText size={20} className="muted" />
              <div className="upload-detail">
                <strong>{item.file.name}</strong>
                <div className="upload-meta">
                  <span>{(item.file.size / 1024 / 1024).toFixed(1)} MB</span>
                  <Tag
                    color={
                      item.state === "failed"
                        ? "error"
                        : ["accepted", "duplicate"].includes(item.state)
                          ? "success"
                          : "default"
                    }
                  >
                    {item.state === "uploading" && item.progress === 100
                      ? "等待接收"
                      : labels[item.state]}
                  </Tag>
                  {item.result?.job_id && (
                    <Link
                      to={`/imports/jobs?job=${encodeURIComponent(item.result.job_id)}`}
                    >
                      查看任务
                    </Link>
                  )}
                  {!item.result?.job_id && item.result?.paper_id && (
                    <Link to={readerPath(item.result.paper_id)}>阅读论文</Link>
                  )}
                </div>
                {item.state === "uploading" && (
                  <Progress
                    percent={item.progress}
                    size="small"
                    showInfo={false}
                  />
                )}
                {item.error && <p className="upload-error">{item.error}</p>}
              </div>
              {["failed", "stopped"].includes(item.state) && (
                <Button size="small" onClick={() => queue.retry(item.id)}>
                  重新排队
                </Button>
              )}
              {!["uploading", "backoff"].includes(item.state) && (
                <Button
                  aria-label={`移除 ${item.file.name}`}
                  type="text"
                  icon={<X size={16} />}
                  onClick={() => queue.remove(item.id)}
                />
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="url-import">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submitUrl();
            }}
          >
            <label htmlFor="pdf-url">PDF 链接</label>
            <Input
              id="pdf-url"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.org/paper.pdf"
              required
            />
            <Button
              type="primary"
              htmlType="submit"
              loading={submitting}
              icon={<UploadCloud size={16} />}
            >
              提交导入
            </Button>
          </form>
          {accepted && (
            <div className="accepted-result">
              <Tag color="success">
                {accepted.duplicate ? "已在库中" : "已接收"}
              </Tag>
              <Link
                to={`/imports/jobs?job=${encodeURIComponent(accepted.job_id)}`}
              >
                查看任务 <ArrowUpRight size={14} />
              </Link>
            </div>
          )}
        </div>
      )}
    </>
  );
}
