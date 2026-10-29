import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Upload,
} from "antd";
import { Check, FileJson, ScanLine, UploadCloud, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, errorMessage, request } from "../api";
import { Blank, Failure, PageHeader } from "../components";
import type { MetadataImportReport, MetadataSource } from "../contracts";

const fieldLabels: Record<string, string> = {
  source_ref: "来源记录",
  source_id: "来源 ID",
  paper_id: "论文 ID",
  match_status: "匹配状态",
  match_method: "匹配方式",
  match_confidence: "置信度",
  reason: "原因",
  note: "备注",
  evidence: "匹配证据",
  match_conflicts: "身份冲突",
  candidates: "候选论文",
  field: "字段",
  kept: "保留值",
  rejected: "导入值",
  scheme: "标识符类型",
  value: "标识符",
  source_type: "来源类型",
};

export default function MetadataImport() {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<MetadataImportReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"preview" | "apply" | null>(null);
  const [canApply, setCanApply] = useState(false);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(
    null,
  );
  const guard = useRef(false);
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);
  function choose(value: File | null) {
    setFile(value);
    setReport(null);
    setError("");
    setCanApply(false);
    setSelected(null);
  }
  async function submit(apply: boolean) {
    if (!file || guard.current || (apply && !canApply)) return;
    guard.current = true;
    setBusy(apply ? "apply" : "preview");
    setCanApply(false);
    setError("");
    setReport(null);
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await request<MetadataImportReport>(
        `/api/metadata/import?apply=${apply}&source_type=import_file`,
        { method: "POST", body },
        120000,
      );
      setReport(result);
      setCanApply(result.dry_run && result.total > 0);
    } catch (error) {
      setError(
        apply && (!(error instanceof ApiError) || error.status >= 500)
          ? `${errorMessage(error)}。导入结果未确认，请先检查论文库或重新预览，避免重复提交。`
          : errorMessage(error),
      );
    } finally {
      guard.current = false;
      setBusy(null);
    }
  }
  return (
    <>
      <PageHeader
        title="元数据导入"
        description="外部元数据与来源记录"
        actions={<Link to="/library">论文库</Link>}
      />
      <div className="metadata-upload">
        <Upload.Dragger
          accept=".json,.jsonl,.ndjson"
          multiple={false}
          disabled={!!busy}
          showUploadList={false}
          beforeUpload={(value) => {
            if (!/\.(json|jsonl|ndjson)$/i.test(value.name)) {
              choose(null);
              setError("请选择 JSON、JSONL 或 NDJSON 文件");
              return Upload.LIST_IGNORE;
            }
            choose(value);
            return false;
          }}
        >
          <FileJson size={32} className="upload-mark" />
          <p className="upload-title">{file?.name || "选择或拖放元数据文件"}</p>
          <p className="muted">
            {file
              ? `${(file.size / 1024).toFixed(1)} KB`
              : "JSON / JSONL / NDJSON · IEEE / CSL / 通用元数据"}
          </p>
        </Upload.Dragger>
      </div>
      <div className="metadata-actions">
        <Tooltip title="移除文件">
          <Button
            aria-label="移除元数据文件"
            icon={<X size={16} />}
            disabled={!file || !!busy}
            onClick={() => choose(null)}
          />
        </Tooltip>
        <Button
          icon={<ScanLine size={16} />}
          disabled={!file || !!busy}
          loading={busy === "preview"}
          onClick={() => void submit(false)}
        >
          预览导入
        </Button>
        <Button
          type="primary"
          icon={<UploadCloud size={16} />}
          disabled={!canApply || !!busy}
          loading={busy === "apply"}
          onClick={() => void submit(true)}
        >
          正式导入
        </Button>
      </div>
      {error && <Failure message={error} />}
      {busy && (
        <Alert
          type="info"
          showIcon
          title={busy === "preview" ? "正在预览元数据匹配…" : "正在写入元数据…"}
        />
      )}
      {!report && !busy && !error && (
        <Blank text={file ? "尚未预览" : "尚未选择元数据文件"} />
      )}
      {report && (
        <>
          <div className="section-heading">
            <h2>{report.dry_run ? "导入预览" : "导入报告"}</h2>
            <Tag color={report.dry_run ? "blue" : "success"}>
              {report.dry_run ? "未写入" : "已处理"}
            </Tag>
            <Tag>{report.format}</Tag>
          </div>
          <div className="data-summary" aria-label="元数据导入统计">
            {(
              [
                ["检测到", report.detected],
                ["参与匹配", report.total],
                ["匹配论文", report.matched],
                [
                  report.dry_run ? "拟新增论文" : "新增论文",
                  report.created_shell,
                ],
                ["需复核", report.ambiguous],
                ["未匹配", report.unmatched],
                ["已导入", report.unchanged],
                ["失败", report.failed],
                ["跳过", report.skipped],
              ] as const
            ).map(([label, count]) => (
              <span key={label}>
                {label} <strong>{count}</strong>
              </span>
            ))}
          </div>
          {report.created_shell > 0 && (
            <Alert
              type="info"
              showIcon
              title={`${report.dry_run ? "将新增" : "已新增"} ${report.created_shell} 篇论文，待补充 PDF 原文`}
            />
          )}
          {(report.failed > 0 ||
            report.ambiguous > 0 ||
            report.conflicts.length > 0) && (
            <Alert
              type="warning"
              showIcon
              title="报告包含失败、歧义或字段冲突，请查看明细"
            />
          )}
          <Tabs
            items={[
              {
                key: "sources",
                label: `记录 (${report.sources.length})`,
                children: (
                  <Table<MetadataSource>
                    className="data-table"
                    rowKey="key"
                    dataSource={report.sources.map((source, key) => ({
                      ...source,
                      key,
                    }))}
                    scroll={{ x: 650 }}
                    pagination={{ pageSize: 20, showSizeChanger: false }}
                    columns={[
                      {
                        title: "来源记录",
                        dataIndex: "source_ref",
                        render: (value) => (
                          <span className="metadata-reference">{value}</span>
                        ),
                      },
                      {
                        title: "匹配状态",
                        width: 130,
                        render: (_, source) => (
                          <Tag>
                            {source.note === "already imported"
                              ? "已导入"
                              : source.match_method === "shell"
                                ? "待补原文"
                                : source.match_status === "matched"
                                  ? "已匹配"
                                  : source.match_status === "ambiguous"
                                    ? "需复核"
                                    : source.match_status || "待匹配"}
                          </Tag>
                        ),
                      },
                      { title: "依据", dataIndex: "match_method", width: 150 },
                      {
                        title: "",
                        width: 80,
                        render: (_, source) => (
                          <Button
                            type="link"
                            onClick={() => setSelected(source)}
                          >
                            详情
                          </Button>
                        ),
                      },
                    ]}
                  />
                ),
              },
              {
                key: "failures",
                label: `失败 (${report.failures.length})`,
                children: (
                  <Table
                    className="data-table"
                    rowKey="index"
                    dataSource={report.failures}
                    scroll={{ x: 500 }}
                    columns={[
                      { title: "记录索引", dataIndex: "index", width: 100 },
                      { title: "标识", dataIndex: "identifier" },
                      { title: "原因", dataIndex: "reason" },
                    ]}
                  />
                ),
              },
              {
                key: "conflicts",
                label: `冲突 (${report.conflicts.length})`,
                children: (
                  <Table<Record<string, unknown>>
                    className="data-table"
                    rowKey="key"
                    dataSource={report.conflicts.map((conflict, key) => ({
                      ...conflict,
                      key,
                    }))}
                    scroll={{ x: 550 }}
                    columns={[
                      {
                        title: "字段",
                        render: (_, conflict) =>
                          String(conflict.field || conflict.scheme || "—"),
                      },
                      {
                        title: "保留值",
                        render: (_, conflict) =>
                          JSON.stringify(conflict.kept ?? null),
                      },
                      {
                        title: "导入值",
                        render: (_, conflict) =>
                          JSON.stringify(
                            conflict.rejected ?? conflict.value ?? null,
                          ),
                      },
                      {
                        title: "",
                        render: (_, conflict) => (
                          <Button
                            type="link"
                            onClick={() => setSelected(conflict)}
                          >
                            详情
                          </Button>
                        ),
                      },
                    ]}
                  />
                ),
              },
            ]}
          />
          {!report.dry_run && (
            <Link className="inline" to="/library">
              <Check size={16} />
              查看论文库
            </Link>
          )}
        </>
      )}
      <Drawer
        title="记录详情"
        open={!!selected}
        onClose={() => setSelected(null)}
      >
        <Descriptions
          column={1}
          layout="vertical"
          items={Object.entries(selected || {}).map(([key, value]) => ({
            key,
            label: fieldLabels[key] || key,
            children: (
              <span className="metadata-detail">
                {value == null
                  ? "—"
                  : typeof value === "object"
                    ? JSON.stringify(value, null, 2)
                    : String(value)}
              </span>
            ),
          }))}
        />
      </Drawer>
    </>
  );
}
