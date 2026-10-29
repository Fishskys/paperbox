import { Alert, Button, Empty, Tag } from "antd";
import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">PAPERBOX WORKSPACE</div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <div className="page-actions">{actions}</div>
    </header>
  );
}
export function Failure({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <Alert
      type="error"
      showIcon
      title={message}
      action={
        retry && (
          <Button size="small" icon={<RefreshCw size={14} />} onClick={retry}>
            重试
          </Button>
        )
      }
    />
  );
}
export function Blank({ text }: { text: string }) {
  return (
    <div className="blank">
      <Empty description={text} image={Empty.PRESENTED_IMAGE_SIMPLE} />
    </div>
  );
}

const stages: Record<string, string> = {
  RECEIVED: "已接收",
  QUEUED: "排队中",
  DOWNLOADING: "下载中",
  STORED: "已存储",
  PARSING: "解析中",
  CHUNKING: "切块中",
  EMBEDDING: "向量化中",
  INDEXING: "索引中",
  COMPLETED: "已完成",
  FAILED: "失败",
  AWAITING_FILE: "待补原文",
  ready: "可阅读",
  indexed: "已索引",
  INDEXED: "已索引",
};
export function Stage({ value }: { value: string }) {
  return (
    <Tag
      color={
        value === "FAILED"
          ? "error"
          : ["COMPLETED", "INDEXED", "ready", "indexed"].includes(value)
            ? "success"
            : "default"
      }
    >
      {stages[value] || value}
    </Tag>
  );
}
export function formatTime(value?: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
}
