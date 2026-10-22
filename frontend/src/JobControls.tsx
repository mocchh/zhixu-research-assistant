import { useState } from 'react';
import { Pause, Play, X } from 'lucide-react';
import { errorText, post, type Job } from './api';
import { useWorkspace } from './store';
export default function JobControls({ job }: { job: Job }) {
  const { refresh, notify } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const perform = async (action: 'pause' | 'resume' | 'cancel') => {
    setBusy(true);
    try { await post(`/jobs/${job.id}/${action}`); await refresh(); notify(action === 'pause' ? '任务将在当前分块调用完成后暂停。' : action === 'cancel' ? '任务将在当前分块调用完成后取消；已有结果保留。' : '任务已恢复，将复用已完成结果。'); }
    catch (error) { notify(errorText(error), 'error'); } finally { setBusy(false); }
  };
  const active = ['queued', 'pending', 'processing', 'running'].includes(job.status);
  const resumable = ['paused', 'error'].includes(job.status);
  return <div className="job-controls">{active && <button className="button small" disabled={busy} onClick={() => void perform('pause')}><Pause size={12}/>暂停</button>}{resumable && <button className="button small" disabled={busy} onClick={() => void perform('resume')}><Play size={12}/>恢复</button>}{(active || resumable || job.status === 'pause_requested') && <button className="button small" disabled={busy} onClick={() => void perform('cancel')}><X size={12}/>取消</button>}{job.status === 'pause_requested' && <small>等待当前分块后暂停</small>}{job.status === 'cancel_requested' && <small>等待当前分块后取消</small>}{job.status === 'cancelled' && <small>已取消</small>}</div>;
}
