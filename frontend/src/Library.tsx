import JobControls from './JobControls';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowRight, BookOpen, Check, ChevronDown, FilePlus2, Files, FolderOpen, HardDrive, MoreHorizontal, Plus, RefreshCw, Search, Sparkles, Star, Trash2, Upload, X } from 'lucide-react';
import { actionable, api, errorText, failedImport, post, titleOf, type ImportResult, type Job, type JobListing, type Paper } from './api';
import { useWorkspace } from './store';
import { Empty, ErrorBox, Modal, Spinner, Status } from './ui';

export default function Library() {
  const { papers, jobs, stats, loading, selected, setSelected, openPaper, setView, upload, uploading, importResult, lastImport, dismissImport, refresh, notify, toggleFavorite, retry, health } = useWorkspace();
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'favorite' | 'processing'>('all');
  const [dragging, setDragging] = useState(false);
  const [showSamples, setShowSamples] = useState(false);
  const [samples, setSamples] = useState<{ filename: string; size: number }[]>([]);
  const [chosenSamples, setChosenSamples] = useState<string[]>([]);
  const [samplesLoading, setSamplesLoading] = useState(false);
  const [sampleError, setSampleError] = useState('');
  const [importing, setImporting] = useState(false);
  const [deletePaper, setDeletePaper] = useState<Paper | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [showJobs, setShowJobs] = useState(false);
  const [historyCursors, setHistoryCursors] = useState<(string | null)[]>([null]);
  const [history, setHistory] = useState<JobListing | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const historyCursor = historyCursors[historyCursors.length - 1];
  const input = useRef<HTMLInputElement>(null);
  const activeJobs = jobs.filter(actionable);
  useEffect(() => {
    if (!showJobs) return;
    const controller = new AbortController();
    setHistoryLoading(true); setHistoryError(''); setHistory(null);
    api<JobListing>(`/jobs?limit=20${historyCursor ? `&cursor=${encodeURIComponent(historyCursor)}` : ''}`, {signal:controller.signal})
      .then(result => { if (!controller.signal.aborted) setHistory(result); }).catch(error => { if (!controller.signal.aborted) setHistoryError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [showJobs, historyCursor, jobs]);
  const filtered = useMemo(() => papers.filter(paper => (filter !== 'favorite' || paper.is_favorite) && (filter !== 'processing' || ['queued', 'processing'].includes(paper.status)) && `${paper.title} ${paper.filename} ${paper.authors.join(' ')}`.toLowerCase().includes(query.toLowerCase())), [papers, filter, query]);
  const allChecked = filtered.length > 0 && filtered.every(paper => selected.includes(paper.id));
  const loadSamples = async () => {
    setShowSamples(true); setSamplesLoading(true); setSampleError('');
    try { setSamples((await api<{ files: typeof samples }>('/samples')).files); }
    catch (error) { setSampleError(errorText(error)); } finally { setSamplesLoading(false); }
  };
  const importSamples = async () => {
    setImporting(true); setSampleError('');
    try { await importResult(await post<ImportResult>('/samples/import', chosenSamples.length ? { filenames: chosenSamples } : {})); setShowSamples(false); setChosenSamples([]); }
    catch (error) { const result = failedImport(error); if (result) await importResult(result); setSampleError(errorText(error)); }
    finally { await refresh(); setImporting(false); }
  };
  const rebuild = async (paper: Paper) => {
    try { await post(`/papers/${paper.id}/reparse`); notify('已从首页重建解析，原始 PDF、会话和笔记保留。'); await refresh(); }
    catch (error) { notify(errorText(error), 'error'); }
  };
  const confirmDelete = async () => {
    if (!deletePaper) return;
    setDeleting(true);
    try { await api(`/papers/${deletePaper.id}`, { method: 'DELETE' }); setDeletePaper(null); notify('文献已从工作台移除。'); await refresh(); }
    catch (error) { notify(errorText(error), 'error'); } finally { setDeleting(false); }
  };
  const jobRow = (job: Job) => <div className="job-item" key={job.id}><div><strong>{job.kind === 'parse' ? '文献解析' : job.kind.includes('translat') ? '全文翻译' : job.kind}</strong>{job.paper_id && <span>{papers.find(paper => paper.id === job.paper_id)?.title || '文献已移除'}</span>}<span>{job.message || job.status}</span></div><div className="job-meter"><div className="progress"><span style={{width:`${Math.min(100, Math.max(0, job.progress))}%`}} /></div><small>{Math.round(job.progress)}%</small></div><JobControls job={job}/></div>;
  return <div className={`page library-page ${dragging ? 'dragging' : ''}`} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void upload(Array.from(event.dataTransfer.files)); }}>
    <div className="page-heading"><div><div className="eyebrow">YOUR RESEARCH, CONNECTED</div><h1>文献库<span className="title-period">.</span></h1><p>从一篇论文出发，让知识之间的联系变得清晰。</p></div><div className="heading-actions"><button className="button" onClick={() => void loadSamples()}><FolderOpen size={16} />导入样例</button><button className="button primary" disabled={uploading} onClick={() => input.current?.click()}>{uploading ? <Spinner label="上传中" /> : <><Plus size={17} />上传文献</>}</button><input ref={input} type="file" accept=".pdf,application/pdf" multiple hidden onChange={event => { if (event.target.files) void upload(Array.from(event.target.files)); event.target.value = ''; }} /></div></div>
    {!!lastImport?.errors?.length && <section className="notice error" aria-label="导入结果"><div><strong>已接收 {lastImport.papers.length} 篇，跳过 {lastImport.duplicates.length} 个重复文件，{lastImport.errors.length} 个文件失败</strong><ul>{lastImport.errors.map((item, index) => <li key={`${item.filename}-${index}`}><strong>{item.filename}</strong>：{item.detail}</li>)}</ul><p>成功文件已经保存，可单独重试失败文件。</p><button className="button small" onClick={dismissImport}>关闭导入结果</button></div></section>}
    <section className="overview"><div className="overview-intro"><span className="overview-icon"><BookOpen size={23} /></span><div><h2>你的研究，从这里积累</h2><p>阅读 · 提问 · 连接 · 创作</p></div><span className="overview-decoration">✳</span></div><div className="stat"><span>已收录文献</span><strong>{stats?.papers ?? papers.length}<small>篇</small></strong></div><div className="stat"><span>已解析页面</span><strong>{stats?.pages ?? papers.reduce((sum, p) => sum + p.processed_pages, 0)}<small>页</small></strong></div><div className="stat"><span>正在处理</span><strong>{stats?.processing ?? papers.filter(p => ['queued', 'processing'].includes(p.status)).length}<small>篇</small></strong></div></section>
    <div className="library-toolbar"><div className="tabs"><button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部文献 <span>{papers.length}</span></button><button className={filter === 'favorite' ? 'active' : ''} onClick={() => setFilter('favorite')}><Star size={14} />我的收藏</button><button className={filter === 'processing' ? 'active' : ''} onClick={() => setFilter('processing')}>处理中</button></div><div className="toolbar-right"><label className="search"><Search size={16} /><input placeholder="搜索标题、作者或文件名" value={query} onChange={event => setQuery(event.target.value)} aria-label="搜索文献" />{query && <button onClick={() => setQuery('')} aria-label="清空搜索"><X size={13} /></button>}</label><button className="icon-button" onClick={() => void refresh()} aria-label="刷新文献"><RefreshCw size={16} /></button></div></div>
    {selected.length > 0 && <div className="selection-bar"><span><Check size={15} />已选择 {selected.length} 篇文献</span><div><button onClick={() => setView('chat')}>基于所选提问 <ArrowRight size={14} /></button><button onClick={() => setView('research')}>生成研究笔记</button><button aria-label="取消选择" onClick={() => setSelected([])}><X size={16} /></button></div></div>}
    <section className="paper-table"><div className="paper-table-head"><label><input type="checkbox" aria-label="选择本页全部文献" checked={allChecked} onChange={() => setSelected(previous => allChecked ? previous.filter(id => !filtered.some(p => p.id === id)) : [...new Set([...previous, ...filtered.map(p => p.id)])])} /><span>文献名称</span></label><span>状态</span><span>页数</span><span>操作</span></div>
      {loading ? <div className="loading-area"><Spinner label="正在读取文献库…" /></div> : filtered.length ? filtered.map(paper => <article className={`paper-row ${selected.includes(paper.id) ? 'checked' : ''}`} key={paper.id}><div className="paper-main"><input type="checkbox" checked={selected.includes(paper.id)} aria-label={`选择 ${titleOf(paper)}`} onChange={() => setSelected(previous => previous.includes(paper.id) ? previous.filter(id => id !== paper.id) : [...previous, paper.id])} /><button className="paper-file-icon" onClick={() => openPaper(paper.id)} aria-label={`阅读 ${titleOf(paper)}`}><Files size={22} /><small>PDF</small></button><div className="paper-info"><button className="paper-title" onClick={() => openPaper(paper.id)}>{titleOf(paper)}</button><p>{paper.authors.length ? paper.authors.slice(0, 3).join(' · ') + (paper.authors.length > 3 ? ' 等' : '') : paper.filename}</p><div className="paper-meta">{paper.arxiv_id && <span>arXiv:{paper.arxiv_id}</span>}{paper.version && <span className="version-chip">v{paper.version}</span>}{paper.source === 'sample' && <span>示例文献</span>}{paper.status === 'ready' && health?.parser_version && paper.parser_version !== health.parser_version && <span className="version-chip amber">可更新解析</span>}{paper.ocr_pages > 0 && <span>OCR {paper.ocr_pages} 页</span>}{paper.duplicate_of && <span className="version-chip amber">同论文不同版本</span>}{paper.tags?.map(tag => <span className="tag" key={tag}>{tag}</span>)}</div>{paper.error && <p className="paper-error">{paper.error}</p>}{paper.warnings?.length > 0 && <details className="paper-warnings"><summary>{paper.warnings.length} 条解析提示</summary>{paper.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}</div></div><div className="paper-status"><Status paper={paper} />{['processing', 'queued'].includes(paper.status) && <small>{paper.processed_pages} / {paper.page_count || '—'} 页</small>}</div><span className="page-count">{paper.page_count || '—'}</span><div className="paper-actions"><button className={`icon-button ${paper.is_favorite ? 'favorited' : ''}`} aria-label={paper.is_favorite ? '取消收藏' : '收藏文献'} onClick={() => void toggleFavorite(paper)}><Star size={16} fill={paper.is_favorite ? 'currentColor' : 'none'} /></button><details className="row-menu"><summary aria-label="更多操作"><MoreHorizontal size={18} /></summary><div><button onClick={() => openPaper(paper.id)}><BookOpen size={14} />打开文献</button><a href={`/api/papers/${paper.id}/file`} target="_blank" rel="noreferrer"><ArrowDownToLine size={14} />原始 PDF</a>{['error', 'partial', 'cancelled'].includes(paper.status) && <button onClick={() => void retry(paper)}><RefreshCw size={14} />重新解析</button>}{['ready', 'partial', 'error', 'cancelled'].includes(paper.status) && <button onClick={() => void rebuild(paper)}><RefreshCw size={14} />从首页重建解析</button>}{paper.status === 'paused' && <button onClick={() => { setShowJobs(true); setTimeout(() => document.getElementById('background-jobs')?.scrollIntoView({behavior:'instant',block:'start'}), 0); }}><RefreshCw size={14}/>到任务列表恢复</button>}<button className="danger-text" onClick={() => setDeletePaper(paper)}><Trash2 size={14} />删除文献</button></div></details></div></article>) : <Empty title={query || filter !== 'all' ? '没有找到匹配的文献' : '建立你的第一座文献库'} text={query || filter !== 'all' ? '试试其他关键词，或切换到全部文献。' : '上传 PDF，或从内置论文中开始。解析完成后，你可以阅读、提问与追溯证据。'} action={!query && filter === 'all' ? <button className="button primary" onClick={() => input.current?.click()}><Upload size={15} />上传 PDF</button> : undefined} />}
    </section>
    <button className="drop-strip" disabled={uploading} onClick={() => input.current?.click()}><FilePlus2 size={21} /><span><strong>继续添加你的研究素材</strong>将 PDF 拖放至此，支持批量上传与版本识别。</span><Plus size={18} /></button>
    <div className="library-bottom"><span><HardDrive size={13} />原始文件独立保存 · 答案可追溯至文献</span><button className="text-button" onClick={() => setShowJobs(!showJobs)}>后台任务 {activeJobs.length > 0 && `(${activeJobs.length})`}<ChevronDown size={14} className={showJobs ? 'rotate' : ''} /></button></div>
    {(showJobs || activeJobs.length > 0) && <section id="background-jobs" className="jobs-panel"><h3>进行中与待处理任务 <span>{activeJobs.length}</span></h3>{activeJobs.length ? activeJobs.map(jobRow) : <p className="muted-text">没有待处理任务。</p>}{showJobs && <><h3>历史任务{history && <span>{history.history_total}</span>}</h3>{historyError && <ErrorBox>{historyError}<button className="button small" onClick={() => void refresh()}>重新加载任务</button></ErrorBox>}{historyLoading ? <Spinner label="正在加载历史任务…"/> : history && <>{history.history_jobs.length ? history.history_jobs.filter(job => !activeJobs.some(active => active.id === job.id)).map(jobRow) : <p className="muted-text">暂无历史任务。</p>}<div className="library-bottom"><button className="button small" disabled={historyCursors.length <= 1} onClick={() => setHistoryCursors(value => value.slice(0, -1))}>上一页历史</button><span>第 {historyCursors.length} 页 · 每页 20 项</span><button className="button small" disabled={!history.next_cursor} onClick={() => {if(history.next_cursor) setHistoryCursors(value => [...value, history.next_cursor]);}}>下一页历史</button></div></>}</>}</section>}
    {dragging && <div className="drag-overlay"><Upload size={40} /><h2>放下 PDF，开始连接知识</h2><p>支持一次上传多篇文献</p></div>}
    {showSamples && <Modal title="从内置论文开始" close={() => { if (!importing) setShowSamples(false); }}><p className="modal-description">选取项目中的真实 PDF。未选择时，导入默认样例。</p>{sampleError && <ErrorBox>{sampleError}</ErrorBox>}{samplesLoading ? <div className="loading-area"><Spinner /></div> : <div className="sample-list">{samples.map(sample => <label key={sample.filename}><input type="checkbox" checked={chosenSamples.includes(sample.filename)} onChange={() => setChosenSamples(previous => previous.includes(sample.filename) ? previous.filter(name => name !== sample.filename) : [...previous, sample.filename])} /><Files size={18} /><span>{sample.filename}<small>{(sample.size / 1024 / 1024).toFixed(1)} MB</small></span></label>)}{!samples.length && !sampleError && <p className="muted-text">没有找到可导入的样例文件。</p>}</div>}<div className="modal-footer"><span>{chosenSamples.length ? `已选 ${chosenSamples.length} 个文件` : '导入默认样例'}</span><button className="button primary" disabled={importing || samplesLoading || !samples.length} onClick={() => void importSamples()}>{importing ? <Spinner label="正在导入" /> : <><Sparkles size={15} />开始导入</>}</button></div></Modal>}
    {deletePaper && <Modal title="删除这篇文献？" close={() => { if (!deleting) setDeletePaper(null); }}><p className="modal-description">将删除工作台中的文献与解析数据。项目目录中的原始 PDF 将保留。</p><div className="delete-paper"><Files size={21} /><strong>{titleOf(deletePaper)}</strong></div><div className="modal-footer"><button className="button" disabled={deleting} onClick={() => setDeletePaper(null)}>取消</button><button className="button danger" disabled={deleting} onClick={() => void confirmDelete()}>{deleting ? <Spinner label="正在删除" /> : '确认删除'}</button></div></Modal>}
  </div>;
}
