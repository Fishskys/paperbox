import { App, Button, Drawer, Progress, Select, Table, Tooltip } from 'antd';
import { ArrowUpRight, RefreshCw, RotateCcw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { errorMessage, post, request, useCredentialRevision } from '../api';
import { Failure, formatTime, PageHeader, Stage } from '../components';
import type { Job, JobList } from '../contracts';
import { useResource, useVisible } from '../hooks';
import { positivePage, readerPath } from '../search-model';

const stages = ['RECEIVED', 'QUEUED', 'DOWNLOADING', 'STORED', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'COMPLETED', 'FAILED'];
const active = (job: Job) => !['COMPLETED', 'FAILED'].includes(job.stage);

export default function Jobs() {
  const [params, setParams] = useSearchParams();
  const encoded = params.toString();
  const page = positivePage(params.get('page'));
  const visible = useVisible();
  const revision = useCredentialRevision();
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{ data?: JobList; error?: string; loading: boolean; checked?: string; query?: string }>({ loading: true });
  const [retrying, setRetrying] = useState<string | null>(null);
  const { message } = App.useApp();
  const detail = useResource<Job>(params.get('job') ? `/api/jobs/${encodeURIComponent(params.get('job')!)}` : null, refresh);
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ limit: '20', offset: String((positivePage(new URLSearchParams(encoded).get('page')) - 1) * 20) });
    const stage = new URLSearchParams(encoded).get('stage');
    if (stage) query.set('stage', stage);
    setState((previous) => previous.query === encoded ? { ...previous, loading: true, error: undefined } : { query: encoded, loading: true });
    async function load() {
      try {
        const data = await request<JobList>(`/api/jobs?${query}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState({ data, query: encoded, loading: false, checked: new Date().toISOString() });
      } catch (error) {
        if (!controller.signal.aborted) setState((previous) => ({ ...previous, loading: false, error: errorMessage(error) }));
      }
    }
    if (visible) void load();
    else setState((previous) => ({ ...previous, loading: false }));
    return () => controller.abort();
  }, [encoded, refresh, visible, revision]);
  useEffect(() => {
    if (!visible || state.error || !(state.data?.jobs.some(active) || (detail.data && active(detail.data)))) return;
    const timer = setTimeout(() => setRefresh((v) => v + 1), 3000);
    return () => clearTimeout(timer);
  }, [visible, state.data, state.error, detail.data]);
  async function retry(job: Job) {
    setRetrying(job.job_id);
    try { await post(`/api/jobs/${encodeURIComponent(job.job_id)}/retry`, {}); setRefresh((v) => v + 1); }
    catch (error) { void message.error(errorMessage(error)); }
    finally { setRetrying(null); }
  }
  function show(job: Job) { const next = new URLSearchParams(params); next.set('job', job.job_id); setParams(next); }
  return <>
    <PageHeader title="导入任务" description={state.checked ? `更新于 ${formatTime(state.checked)}` : '处理队列'} actions={<Button icon={<RefreshCw size={16} />} loading={state.loading} onClick={() => setRefresh((v) => v + 1)}>刷新</Button>} />
    <div className="list-toolbar"><Select aria-label="任务阶段" value={params.get('stage') || ''} options={[{ label: '全部阶段', value: '' }, ...stages.map((stage) => ({ label: <Stage value={stage} />, value: stage }))]} onChange={(value) => { const next = new URLSearchParams(params); if (value) next.set('stage', value); else next.delete('stage'); next.delete('page'); setParams(next); }} /></div>
    {state.error && <Failure message={state.error} retry={() => setRefresh((v) => v + 1)} />}
    <Table<Job> className="data-table" rowKey="job_id" dataSource={state.data?.jobs || []} loading={state.loading && !state.data} scroll={{ x: 750 }} pagination={{ current: page, pageSize: 20, total: state.data?.total || 0, showSizeChanger: false, onChange: (value) => { const next = new URLSearchParams(params); next.set('page', String(value)); setParams(next); } }} locale={{ emptyText: state.error ? '任务列表加载失败' : '暂无任务' }} columns={[
      { title: '任务', key: 'job', render: (_, job) => <div className="paper-cell"><Button type="link" className="job-id" onClick={() => show(job)}>{job.job_id.slice(0, 12)}</Button><p>{job.error_message || (job.paper_id ? `论文 ${job.paper_id.slice(0, 8)}` : '等待关联论文')}</p></div> },
      { title: '阶段', dataIndex: 'stage', width: 115, render: (value: string) => <Stage value={value} /> },
      { title: '进度', key: 'progress', width: 140, render: (_, job) => <Progress size="small" percent={Math.max(0, Math.min(100, job.progress))} status={job.stage === 'FAILED' ? 'exception' : job.stage === 'COMPLETED' ? 'success' : 'normal'} /> },
      { title: '更新时间', dataIndex: 'updated_at', width: 180, render: (value: string) => formatTime(value) },
      { title: '', key: 'actions', width: 95, render: (_, job) => <div className="inline">{job.stage === 'FAILED' && <Tooltip title="重试任务"><Button type="text" aria-label={`重试任务 ${job.job_id}`} loading={retrying === job.job_id} disabled={retrying !== null} icon={<RotateCcw size={16} />} onClick={() => void retry(job)} /></Tooltip>}{job.paper_id && job.stage === 'COMPLETED' && <Tooltip title="打开论文"><Link aria-label="打开论文" to={readerPath(job.paper_id)}><ArrowUpRight size={18} /></Link></Tooltip>}</div> },
    ]} />
    <Drawer title="任务详情" open={!!params.get('job')} onClose={() => { const next = new URLSearchParams(params); next.delete('job'); setParams(next); }}>{detail.error && <Failure message={detail.error} />}{detail.data && <div className="paper-info"><h3>任务 ID</h3><p>{detail.data.job_id}</p><Stage value={detail.data.stage} /><Progress percent={Math.max(0, Math.min(100, detail.data.progress))} /><h3>更新时间</h3><p>{formatTime(detail.data.updated_at)}</p>{detail.data.error_message && <Failure message={detail.data.error_message} />}{detail.data.paper_id && <Link to={readerPath(detail.data.paper_id)}>打开论文</Link>}</div>}</Drawer>
  </>;
}
