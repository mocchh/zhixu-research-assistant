import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowLeft, ChevronRight, FileDiff, Languages, List, Search, X } from 'lucide-react';
import { api, errorText, post, type Evidence, type Paper, type PaperDetail } from './api';
import { useWorkspace } from './store';
import { ErrorBox, Modal, Notice, Spinner } from './ui';
import { downloadApiFile } from './persistence';
import { RequestScope } from './requestScope';

interface Estimate { paper_id: string; start_page: number; end_page: number; pages: number; total_characters: number; pending_characters: number; estimated_input_tokens: number; cached_pages: number; warning: string }
interface Reference { label: string; text: string; page: number; block_id: string; bbox: number[]; arxiv_id?: string; doi?: string; linked_paper_id?: string }
interface ReferenceLink { label: string; source: Evidence; target: Evidence }
interface Comparison { left: Paper; right: Paper; similarity: number; changes: { kind: 'replace' | 'insert' | 'delete'; left_page: number | null; right_page: number | null; before: string; after: string }[]; truncated: boolean; notes: string[] }
export function TranslationPlanner({ paper, language, setLanguage }: { paper: Paper; language: 'zh' | 'en'; setLanguage: (value: 'zh' | 'en') => void }) {
  const { health, setView, notify, refresh } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState(1);
  const [end, setEnd] = useState(paper.page_count);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const version = useRef(0);
  useEffect(() => { setStart(1); setEnd(paper.page_count); setEstimate(null); setError(''); version.current++; }, [paper.id, paper.page_count]);
  useEffect(() => { setEstimate(null); version.current++; }, [start, end, language]);
  const estimatePages = async () => {
    const request = ++version.current; setBusy(true); setError('');
    try { const result = await post<Estimate>(`/papers/${paper.id}/translation-estimate`, { start_page: start, end_page: end, target_language: language }); if (request === version.current) setEstimate(result); }
    catch (error) { if (request === version.current) setError(errorText(error)); } finally { setBusy(false); }
  };
  const createJob = async () => {
    if (!estimate) return; setBusy(true); setError('');
    try { await post(`/papers/${paper.id}/translation-jobs`, { start_page: start, end_page: end, target_language: language }); notify('翻译任务已创建，可在任务列表暂停、恢复或取消。'); setOpen(false); await refresh(); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  };
  return <><div className="translation-controls"><label>目标语言<select aria-label="翻译目标语言" value={language} onChange={event => setLanguage(event.target.value as 'zh' | 'en')}><option value="zh">中文</option><option value="en">英文</option></select></label><button className="button small" onClick={() => setOpen(true)}><Languages size={13}/>范围翻译</button><button className="button small" onClick={() => void downloadApiFile(`/papers/${paper.id}/translations/export?target_language=${language}&format=md`, `${paper.filename}-${language}.md`).catch(error => notify(errorText(error), 'error'))}><ArrowDownToLine size={13}/>译文 MD</button><button className="button small" onClick={() => void downloadApiFile(`/papers/${paper.id}/translations/export?target_language=${language}&format=html`, `${paper.filename}-${language}.html`).catch(error => notify(errorText(error), 'error'))}>译文 HTML</button></div>{open && <Modal title="翻译范围与工作量" close={() => { if (!busy) setOpen(false); }}><div className="modal-content"><p className="muted-text">已有的对应语言译文会复用缓存。任务可在当前分块调用完成后暂停或取消。</p><div className="range-inputs"><label>起始页<input type="number" min={1} max={paper.page_count} value={start} onChange={event => setStart(Number(event.target.value))}/></label><span>—</span><label>结束页<input type="number" min={start} max={paper.page_count} value={end} onChange={event => setEnd(Number(event.target.value))}/></label></div><button className="button" disabled={busy || start < 1 || end < start || end > paper.page_count} onClick={() => void estimatePages()}>估计工作量</button>{error && <ErrorBox>{error}</ErrorBox>}{estimate && <div className="translation-estimate"><strong>{estimate.pages} 页 · {estimate.cached_pages} 页已有译文</strong><p>待处理约 {estimate.pending_characters.toLocaleString()} 字符 / {estimate.estimated_input_tokens.toLocaleString()} 输入 token。</p><Notice>{estimate.warning || '实际费用取决于所选模型、输出长度和服务计费；此处不提供金额承诺。'}</Notice></div>}{!health?.model_configured && <Notice>创建翻译任务需要先配置模型。<button className="text-button" onClick={() => { setOpen(false); setView('settings'); }}>前往设置</button></Notice>}</div><div className="modal-footer"><button className="button" disabled={busy} onClick={() => setOpen(false)}>关闭</button><button className="button primary" disabled={busy || !estimate || !health?.model_configured} onClick={() => void createJob()}>{busy ? <Spinner label="处理中"/> : `翻译为${language === 'zh' ? '中文' : '英文'}`}</button></div></Modal>}</>;
}

export function DocumentTools({ paper, page, detail }: { paper: Paper; page: number; detail: PaperDetail | null }) {
  const { openPaper } = useWorkspace();
  const [panel, setPanel] = useState<'search' | 'references' | null>(null);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<Evidence[]>([]);
  const [total, setTotal] = useState(0);
  const [references, setReferences] = useState<Reference[]>([]);
  const [links, setLinks] = useState<ReferenceLink[]>([]);
  const [returnTo, setReturnTo] = useState<Evidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [comparing, setComparing] = useState(false);
  const [other, setOther] = useState('');
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const searchSequence = useRef(0);
  const comparisonRequest = useRef(new RequestScope());
  const [compareBusy, setCompareBusy] = useState(false);
  useEffect(() => { comparisonRequest.current.cancel(); setComparison(null); setCompareBusy(false); }, [paper.id, other, comparing]);
  useEffect(() => { setMatches([]); setTotal(0); setQuery(''); setReferences([]); setComparison(null); setOther(''); setError(''); searchSequence.current++; }, [paper.id]);
  useEffect(() => {
    if (panel !== 'references') return;
    const controller = new AbortController(); setError(''); setBusy(true);
    Promise.all([api<{ references: Reference[] }>(`/papers/${paper.id}/references`, { signal: controller.signal }), api<{ links: ReferenceLink[] }>(`/papers/${paper.id}/pages/${page}/reference-links`, { signal: controller.signal })]).then(([refs, current]) => { setReferences(refs.references); setLinks(current.links); }).catch(error => { if (!controller.signal.aborted) setError(errorText(error)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [paper.id, page, panel]);
  const search = async () => {
    if (!query.trim()) return; const sequence = ++searchSequence.current; setBusy(true); setError('');
    try { const result = await api<{ matches: Evidence[]; total: number }>(`/papers/${paper.id}/search?q=${encodeURIComponent(query)}&limit=40`); if (sequence === searchSequence.current) { setMatches(result.matches); setTotal(result.total); } }
    catch (error) { if (sequence === searchSequence.current) setError(errorText(error)); } finally { if (sequence === searchSequence.current) setBusy(false); }
  };
  const compare = async () => { if (!other) return; const request = comparisonRequest.current.begin(`${paper.id}:${other}`); setCompareBusy(true); setError(''); try { const value = await api<Comparison>(`/papers/${paper.id}/compare/${other}`, { timeoutMs: 120_000, signal: request.signal }); if (request.isCurrent()) setComparison(value); } catch (error) { if (request.isCurrent()) setError(errorText(error)); } finally { if (request.isCurrent()) setCompareBusy(false); } };
  return <><div className="document-tools"><button className={`button small ${panel === 'search' ? 'selected-button' : ''}`} onClick={() => setPanel(panel === 'search' ? null : 'search')}><Search size={13}/>全文检索</button><button className={`button small ${panel === 'references' ? 'selected-button' : ''}`} onClick={() => setPanel(panel === 'references' ? null : 'references')}><List size={13}/>参考文献</button>{detail?.versions.some(version => version.id !== paper.id) && <button className="button small" onClick={() => { setComparing(true); setComparison(null); }}><FileDiff size={13}/>版本差异</button>}<a className="button small" href={`/api/papers/${paper.id}/citation?format=bibtex`} download><ArrowDownToLine size={13}/>BibTeX</a>{returnTo && <button className="button small" onClick={() => { openPaper(returnTo.paper_id, returnTo.page, returnTo); setReturnTo(null); }}><ArrowLeft size={13}/>返回引用处</button>}</div>{panel && <section className="document-panel"><div className="document-panel-heading"><strong>{panel === 'search' ? '检索本文原文' : '本页引用与参考文献'}</strong><button className="icon-button" onClick={() => setPanel(null)} aria-label="关闭文档工具"><X size={14}/></button></div>{error && <ErrorBox>{error}</ErrorBox>}{panel === 'search' ? <><form className="document-search" onSubmit={event => { event.preventDefault(); void search(); }}><input aria-label="全文检索关键词" value={query} onChange={event => setQuery(event.target.value)} placeholder="输入原文词语或短语"/><button className="button small" disabled={busy || !query.trim()}><Search size={14}/>搜索</button></form>{total > 0 && <p className="muted-text">共 {total} 处，显示前 {matches.length} 处。</p>}<div className="document-results">{matches.map((match, index) => <button key={`${match.id}-${index}`} onClick={() => openPaper(match.paper_id, match.page, match)}><span>第 {match.page} 页 <ChevronRight size={12}/></span><p>{match.text}</p></button>)}</div>{!busy && query && total === 0 && <p className="muted-text">输入关键词后搜索；匹配仅基于已解析的原文文本。</p>}</> : <>{links.length > 0 && <div className="reference-links">{links.map((link, index) => <button className="button small" key={index} onClick={() => { setReturnTo(link.source); openPaper(link.target.paper_id, link.target.page, link.target); }}>[{link.label}] 跳转参考文献</button>)}</div>}<div className="document-results">{references.map((reference, index) => <button key={`${reference.block_id}-${index}`} onClick={() => { const source: Evidence = { id: reference.block_id, paper_id: paper.id, paper_title: paper.title, page: reference.page, text: reference.text, bbox: reference.bbox, score: 1 }; setReturnTo({ ...source, id: 'reading-origin', page, bbox: [] }); openPaper(paper.id, reference.page, source); }}><span>[{reference.label}] · 第 {reference.page} 页</span><p>{reference.text}</p></button>)}</div>{!busy && !references.length && <p className="muted-text">暂未识别到参考文献，仍可结合 PDF 原文查阅。</p>}</>}{busy && <Spinner/>}</section>}{comparing && <Modal title="论文版本差异" close={() => setComparing(false)}><div className="modal-content"><label className="form-label">与当前版本比较</label><select className="form-input" value={other} onChange={event => { setOther(event.target.value); setComparison(null); }}><option value="">选择另一个版本</option>{detail?.versions.filter(version => version.id !== paper.id).map(version => <option key={version.id} value={version.id}>{version.filename}</option>)}</select><button className="button small" disabled={compareBusy || !other} onClick={() => void compare()}>{compareBusy ? <Spinner label="比较中"/> : '比较文本'}</button>{error && <ErrorBox>{error}</ErrorBox>}{comparison && <><p className="comparison-summary">文本相似度 {(comparison.similarity * 100).toFixed(1)}% · {comparison.changes.length} 处差异</p>{comparison.notes.map((note, index) => <Notice key={index}>{note}</Notice>)}{comparison.truncated && <Notice>差异数量较多，当前结果已截断。</Notice>}<div className="version-differences">{comparison.changes.map((change, index) => <article key={index}><strong>{change.kind === 'insert' ? '新增' : change.kind === 'delete' ? '删除' : '修改'}</strong><div><section><button className="text-button" disabled={!change.left_page} onClick={() => { openPaper(comparison.left.id, change.left_page || 1); setComparing(false); }}>当前版本 · 第 {change.left_page ?? '—'} 页</button><p>{change.before || '—'}</p></section><section><button className="text-button" disabled={!change.right_page} onClick={() => { openPaper(comparison.right.id, change.right_page || 1); setComparing(false); }}>比较版本 · 第 {change.right_page ?? '—'} 页</button><p>{change.after || '—'}</p></section></div></article>)}</div></>}</div></Modal>}</>;
}
