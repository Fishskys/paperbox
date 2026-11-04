import { Alert, Button, Progress } from "antd";
import { Download, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAccess } from "./access";
import { errorMessage, post, request } from "./api";
import { downloadJson } from "./files";
import { useVisible } from "./hooks";
import type { MaintenanceJob, MaintenanceRequest } from "./contracts";

type Stored = { jobId?: string; pending?: { request_id: string; [key: string]: unknown } };
const terminal = (job?: MaintenanceJob) => !!job && ["COMPLETED", "FAILED"].includes(job.stage);
function readStored(key: string): Stored {
  try { return JSON.parse(sessionStorage.getItem(key) || "{}"); } catch { return {}; }
}

export function useMaintenanceTask<T>(channel: string) {
  const { context } = useAccess();
  const storageKey = `paperbox:maintenance:${context?.prefix}:${channel}`;
  const [params, setParams] = useSearchParams();
  const [jobId, setJobId] = useState(() => params.get("task") || readStored(storageKey).jobId || "");
  const [job, setJob] = useState<MaintenanceJob>();
  const [report, setReport] = useState<T>();
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retry, setRetry] = useState(0);
  const guard = useRef(false);
  const mounted = useRef(true);
  const visible = useVisible();
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const stored = readStored(storageKey);
        if (!jobId && !stored.pending) return;
        const endpoint = jobId || `requests/${encodeURIComponent(stored.pending!.request_id)}`;
        const value = await request<MaintenanceJob>(`/api/maintenance/jobs/${endpoint}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setJob(value); setError("");
        if (!jobId) {
          sessionStorage.setItem(storageKey, JSON.stringify({ jobId: value.job_id }));
          setJobId(value.job_id);
        }
        if (value.report_available && terminal(value)) {
          const result = await request<T>(`/api/maintenance/jobs/${value.job_id}/report`, { signal: controller.signal });
          if (!controller.signal.aborted) setReport(result);
        }
        if (!terminal(value)) timer = setTimeout(() => void poll(), 2000);
      } catch (error) { if (!controller.signal.aborted) setError(`${errorMessage(error)}；可查询任务状态，写操作不会自动重发。`); }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, visible, retry, storageKey]);

  async function submit(body: MaintenanceRequest) {
    if (guard.current) return;
    guard.current = true; setSubmitting(true); setError(""); setReport(undefined);
    const old = readStored(storageKey).pending;
    const pending = old && JSON.stringify(Object.fromEntries(Object.entries(old).filter(([key]) => key !== "request_id"))) === JSON.stringify(body)
      ? old : { ...body, request_id: crypto.randomUUID() };
    sessionStorage.setItem(storageKey, JSON.stringify({ pending }));
    setJobId(""); setJob(undefined);
    setParams(previous => { const next = new URLSearchParams(previous); next.delete("task"); return next; }, { replace: true });
    try {
      const value = await post<MaintenanceJob>("/api/maintenance/jobs", pending);
      sessionStorage.setItem(storageKey, JSON.stringify({ jobId: value.job_id }));
      if (mounted.current) {
        setJob(value); setJobId(value.job_id); setRetry(v => v + 1);
        setParams(previous => { const next = new URLSearchParams(previous); next.set("task", value.job_id); return next; }, { replace: true });
      }
    } catch (error) { if (mounted.current) setError(`${errorMessage(error)}；提交结果不明，请先查询任务状态。`); }
    finally { guard.current = false; if (mounted.current) setSubmitting(false); }
  }
  return { job, report, error, submit, submitting, active: submitting || (!!job && !terminal(job)), recover: () => setRetry(v => v + 1) };
}

export function MaintenanceTaskStatus({ task }: { task: ReturnType<typeof useMaintenanceTask> }) {
  return <>
    {task.error && <Alert type="error" title={task.error} action={<Button icon={<RefreshCw size={16} />} onClick={task.recover}>查询任务状态</Button>} />}
    {task.job && <div className="metadata-actions">
      <span>任务 {task.job.job_id} · {({ RECEIVED: "已接收", QUEUED: "排队中", PARSING: "执行中", COMPLETED: "已完成", FAILED: "失败" } as Record<string, string>)[task.job.stage] || task.job.stage}</span>
      <Progress percent={Math.round(task.job.progress)} status={task.job.stage === "FAILED" ? "exception" : undefined} style={{ width: 180 }} />
      {task.job.error_message && <Alert type="error" title={`${task.job.error_code || ""} ${task.job.error_message}`} />}
      <Button icon={<RefreshCw size={16} />} onClick={task.recover}>刷新状态</Button>
      {task.report !== undefined && <Button icon={<Download size={16} />} onClick={() => downloadJson(task.report, `maintenance-${task.job!.job_id}.json`)}>下载任务报告</Button>}
    </div>}
  </>;
}
