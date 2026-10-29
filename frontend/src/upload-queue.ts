import { ApiError, authHeaders, detailMessage, errorMessage, markConnection } from './api';
import type { BatchAccepted, FileResult } from './contracts';

export type UploadItem = {
  id: string; file: File; state: 'waiting' | 'uploading' | 'backoff' | 'accepted' | 'duplicate' | 'failed' | 'stopped';
  progress: number; attempt: number; result?: FileResult; error?: string;
};
export type Sender = (file: File, progress: (percent: number) => void) => Promise<BatchAccepted>;

export function retryDelay(value: string | null, attempt: number, now = Date.now()) {
  if (value?.trim()) {
    const seconds = Number(value);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const date = Date.parse(value);
    if (Number.isFinite(date)) return Math.max(0, date - now);
  }
  return Math.min(30000, 1000 * 2 ** (attempt - 1));
}

export const sendFile: Sender = (file, progress) => new Promise((resolve, reject) => {
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/papers/ingest/files');
  xhr.responseType = 'json';
  xhr.timeout = 120000;
  Object.entries(authHeaders()).forEach(([key, value]) => xhr.setRequestHeader(key, value));
  xhr.upload.onprogress = (event) => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
  xhr.onload = () => {
    markConnection('online');
    if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as BatchAccepted);
    else reject(new ApiError(xhr.status, detailMessage(xhr.response), xhr.getResponseHeader('Retry-After')));
  };
  xhr.onerror = xhr.ontimeout = () => { markConnection('offline'); reject(new Error('未收到接收结果，请先检查任务列表再决定是否重传')); };
  const data = new FormData();
  data.append('files', file);
  xhr.send(data);
});

export class UploadQueue {
  private items: UploadItem[] = [];
  private listeners = new Set<() => void>();
  private active = 0;
  private running = false;
  private sequence = 0;
  private waits = new Set<() => void>();
  constructor(private sender: Sender = sendFile) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.items;
  private emit() { this.listeners.forEach((listener) => listener()); }
  private patch(id: string, values: Partial<UploadItem>) { this.items = this.items.map((item) => item.id === id ? { ...item, ...values } : item); this.emit(); }
  add(files: File[]) {
    this.items = [...this.items, ...files.map((file): UploadItem => ({ id: String(++this.sequence), file, state: /\.pdf$/i.test(file.name) ? 'waiting' : 'failed', progress: 0, attempt: 0, ...(!/\.pdf$/i.test(file.name) ? { error: '仅支持 PDF 文件' } : {}) }))];
    this.emit();
    if (this.running) this.pump();
  }
  start() { this.running = true; this.pump(); }
  stopQueued() {
    this.running = false;
    this.items = this.items.map((item) => item.state === 'waiting' || item.state === 'backoff' ? { ...item, state: 'stopped' } : item);
    this.waits.forEach((finish) => finish());
    this.emit();
  }
  remove(id: string) { this.items = this.items.filter((item) => item.id !== id || ['uploading', 'backoff'].includes(item.state)); this.emit(); }
  retry(id: string) { if (this.items.some((item) => item.id === id && /\.pdf$/i.test(item.file.name))) this.patch(id, { state: 'waiting', progress: 0, attempt: 0, error: undefined }); }
  clearFinished() { this.items = this.items.filter((item) => ['waiting', 'uploading', 'backoff'].includes(item.state)); this.emit(); }
  private pump() {
    while (this.running && this.active < 2) {
      const item = this.items.find((entry) => entry.state === 'waiting');
      if (!item) break;
      this.active++;
      this.patch(item.id, { state: 'uploading' });
      void this.run(item).finally(() => { this.active--; this.pump(); });
    }
    if (!this.active && !this.items.some((entry) => entry.state === 'waiting')) this.running = false;
  }
  private wait(delay: number) {
    return new Promise<void>((resolve) => {
      const finish = () => { clearTimeout(timer); this.waits.delete(finish); resolve(); };
      const timer = setTimeout(finish, delay);
      this.waits.add(finish);
    });
  }
  private async run(item: UploadItem) {
    for (let attempt = 1; attempt <= 3; attempt++) {
      this.patch(item.id, { state: 'uploading', attempt, error: undefined });
      try {
        const response = await this.sender(item.file, (progress) => this.patch(item.id, { progress }));
        const result = response.results?.[0];
        if (!result) throw new Error('接收结果缺失，请检查任务列表');
        if (result.status === 'rejected') throw new Error(result.error_message || '文件被拒绝');
        if (!['accepted', 'duplicate'].includes(result.status)) throw new Error(`未知接收状态：${result.status}`);
        this.patch(item.id, { state: result.status as 'accepted' | 'duplicate', result, progress: 100 });
        return;
      } catch (error) {
        if (error instanceof ApiError && error.status === 429 && !this.running) { this.patch(item.id, { state: 'stopped', error: undefined }); return; }
        if (error instanceof ApiError && error.status === 429 && attempt < 3 && this.running) {
          const delay = retryDelay(error.retryAfter, attempt);
          this.patch(item.id, { state: 'backoff', error: `服务繁忙，${Math.ceil(delay / 1000)} 秒后重试` });
          await this.wait(delay);
          if (!this.running) { this.patch(item.id, { state: 'stopped', error: undefined }); return; }
          continue;
        }
        this.patch(item.id, { state: 'failed', error: errorMessage(error) });
        return;
      }
    }
  }
}
