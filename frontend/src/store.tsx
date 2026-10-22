import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { api, errorText, failedImport, post, running, type Evidence, type Health, type ImportResult, type Job, type Paper, type Stats } from './api';

export type View = 'library' | 'reader' | 'chat' | 'graph' | 'discover' | 'research' | 'settings';
interface Workspace {
  view: View; setView: (value: View) => void;
  papers: Paper[]; jobs: Job[]; health: Health | null; stats: Stats | null;
  loading: boolean; connectionError: string; revision: number;
  selected: string[]; setSelected: React.Dispatch<React.SetStateAction<string[]>>;
  paperId: string | null; page: number; setPage: (page: number) => void;
  highlight: Evidence | null; setHighlight: (value: Evidence | null) => void;
  openPaper: (id: string, page?: number, evidence?: Evidence) => void;
  refresh: () => Promise<void>; notify: (message: string, kind?: 'success' | 'error') => void;
  toast: { message: string; kind: 'success' | 'error' } | null;
  uploading: boolean; upload: (files: File[]) => Promise<void>;
  importResult: (result: ImportResult) => Promise<void>;
  lastImport: ImportResult | null; dismissImport: () => void;
  toggleFavorite: (paper: Paper) => Promise<void>;
  retry: (paper: Paper) => Promise<void>;
}
const WorkspaceContext = createContext<Workspace | null>(null);
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<View>('library');
  const [papers, setPapers] = useState<Paper[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [paperId, setPaperId] = useState<string | null>(null);
  const [page, setPageValue] = useState(1);
  const [highlight, setHighlight] = useState<Evidence | null>(null);
  const [toast, setToast] = useState<Workspace['toast']>(null);
  const [uploading, setUploading] = useState(false);
  const [lastImport, setLastImport] = useState<ImportResult | null>(null);
  const toastTimeout = useRef<ReturnType<typeof setTimeout>>();
  const refreshInFlight = useRef<Promise<void> | null>(null);
  const queuedRefresh = useRef<Promise<void> | null>(null);
  const navigationSequence = useRef(0);
  const notify = useCallback((message: string, kind: 'success' | 'error' = 'success') => {
    clearTimeout(toastTimeout.current); setToast({ message, kind });
    toastTimeout.current = setTimeout(() => setToast(null), 6500);
  }, []);
  const refresh = useCallback((): Promise<void> => {
    if (refreshInFlight.current) {
      // A write may have completed after the pending GET took its snapshot.
      // Coalesce callers into one additional read, and let them await that read.
      if (!queuedRefresh.current) queuedRefresh.current = refreshInFlight.current.then(() => {
        queuedRefresh.current = null;
        return refresh();
      });
      return queuedRefresh.current;
    }
    const request = (async () => {
      const results = await Promise.allSettled([
        api<{ papers: Paper[] }>('/papers'), api<{ jobs: Job[] }>('/jobs'), api<Health>('/health'), api<Stats>('/stats'),
      ]);
      const [paperResult, jobResult, healthResult, statsResult] = results;
      if (paperResult.status === 'fulfilled') {
        setPapers(paperResult.value.papers);
        setSelected(ids => ids.filter(id => paperResult.value.papers.some(p => p.id === id)));
        setConnectionError('');
      } else setConnectionError(errorText(paperResult.reason));
      if (jobResult.status === 'fulfilled') setJobs(jobResult.value.jobs);
      if (healthResult.status === 'fulfilled') setHealth(healthResult.value);
      if (statsResult.status === 'fulfilled') setStats(statsResult.value);
      setLoading(false); setRevision(value => value + 1);
    })().finally(() => { refreshInFlight.current = null; });
    refreshInFlight.current = request;
    return request;
  }, []);
  useEffect(() => { void refresh(); return () => clearTimeout(toastTimeout.current); }, [refresh]);
  const hasWork = jobs.some(running) || papers.some(p => ['queued', 'processing'].includes(p.status));
  useEffect(() => {
    if (!hasWork) return;
    const timer = setInterval(() => void refresh(), 2200);
    return () => clearInterval(timer);
  }, [hasWork, refresh]);
  const setPage = (value: number) => { setPageValue(value); setHighlight(null); };
  const openPaper = (id: string, nextPage?: number, evidence?: Evidence) => {
    const sequence = ++navigationSequence.current;
    const show = (position: number) => { if (sequence === navigationSequence.current) { setPaperId(id); setPageValue(position); setHighlight(evidence || null); setView('reader'); } };
    if (nextPage !== undefined) show(nextPage);
    else void api<{page: number}>(`/papers/${id}/reading`).then(position => show(position.page || 1)).catch(() => show(1));
  };
  const importResult = async (result: ImportResult) => {
    const added = result.papers.length;
    const duplicates = result.duplicates.length;
    const failed = result.errors?.length || 0;
    setLastImport(result);
    notify(`已接收 ${added} 篇文献${duplicates ? `；${duplicates} 个重复文件已跳过` : ''}${failed ? `；${failed} 个文件导入失败，请查看下方结果` : ''}`, failed ? 'error' : 'success');
    await refresh();
  };
  const upload = async (files: File[]) => {
    const pdfs = files.filter(file => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'));
    if (!pdfs.length) { notify('请选择 PDF 文件。', 'error'); return; }
    if (pdfs.length !== files.length) notify('已跳过非 PDF 文件。');
    setUploading(true);
    setLastImport(null);
    try {
      const form = new FormData(); pdfs.forEach(file => form.append('files', file));
      await importResult(await api<ImportResult>('/papers/upload', { method: 'POST', body: form }));
    } catch (error) { setLastImport(failedImport(error)); notify(errorText(error), 'error'); await refresh(); }
    finally { setUploading(false); }
  };
  const toggleFavorite = async (paper: Paper) => {
    try { await api<Paper>(`/papers/${paper.id}`, { method: 'PATCH', body: JSON.stringify({ is_favorite: !paper.is_favorite }) }); await refresh(); }
    catch (error) { notify(errorText(error), 'error'); }
  };
  const retry = async (paper: Paper) => {
    try { await post(`/papers/${paper.id}/retry`); notify('已提交重新解析任务。'); await refresh(); }
    catch (error) { notify(errorText(error), 'error'); }
  };
  return <WorkspaceContext.Provider value={{ view, setView, papers, jobs, health, stats, loading, connectionError, revision, selected, setSelected, paperId, page, setPage, highlight, setHighlight, openPaper, refresh, notify, toast, uploading, upload, importResult, lastImport, dismissImport: () => setLastImport(null), toggleFavorite, retry }}>{children}</WorkspaceContext.Provider>;
}
export function useWorkspace() { const context = useContext(WorkspaceContext); if (!context) throw new Error('Workspace missing'); return context; }
