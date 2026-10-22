import { AlertCircle, ArrowUpRight, BookOpen, Check, FileText, LoaderCircle, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Answer, Evidence, Paper } from './api';
import { useWorkspace } from './store';

export function Spinner({ label = '正在加载…' }: { label?: string }) { return <span className="loading"><LoaderCircle size={17} className="spin" />{label}</span>; }
export function Empty({ title, text, action, icon }: { title: string; text: string; action?: ReactNode; icon?: ReactNode }) { return <div className="empty-state"><div className="empty-icon">{icon || <BookOpen size={29} />}</div><h3>{title}</h3><p>{text}</p>{action}</div>; }
export function ErrorBox({ children }: { children: ReactNode }) { return <div className="notice error" role="alert"><AlertCircle size={17} /><span>{children}</span></div>; }
export function Notice({ children }: { children: ReactNode }) { return <div className="notice"><Sparkles size={16} /><span>{children}</span></div>; }
export function Status({ paper }: { paper: Paper }) {
  const labels = { ready: '已就绪', processing: '解析中', queued: '待解析', partial: '部分可用', error: '解析失败', paused: '解析已暂停', cancelled: '解析已取消' };
  return <span className={`status status-${paper.status}`}>{['queued', 'processing'].includes(paper.status) ? <LoaderCircle size={11} className="spin" /> : <span className="status-dot" />}{labels[paper.status]}</span>;
}
export function Modal({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className="modal" onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === ref.current) close(); }}><div className="modal-header"><h2>{title}</h2><button className="icon-button" onClick={close} aria-label="关闭"><X size={20} /></button></div>{children}</dialog>;
}
export function Markdown({ text, onEvidence }: { text: string; onEvidence?: (index: number) => void }) {
  const content = onEvidence ? text.replace(/\[(\d+)\](?!\()/g, '[$1](#evidence-$1)') : text;
  return <div className="markdown"><ReactMarkdown components={{ a: ({ href, children }) => {
    if (href?.startsWith('#evidence-') && onEvidence) return <button className="citation-link" onClick={() => onEvidence(Number(href.slice(10)) - 1)}>{children}</button>;
    return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  } }}>{content}</ReactMarkdown></div>;
}
export function SourceCards({ sources }: { sources: Evidence[] }) {
  const { openPaper } = useWorkspace();
  if (!sources.length) return null;
  return <div className="sources"><div className="eyebrow">可回溯来源 <span>{sources.length}</span></div><div className="source-grid">{sources.map((source, index) => <button className="source-card" key={`${source.id}-${index}`} onClick={() => openPaper(source.paper_id, source.page, source)}><div className="source-heading"><span className="source-number">{index + 1}</span><strong>{source.paper_title}</strong><ArrowUpRight size={15} /></div><p>{source.text}</p><span className="source-footer"><FileText size={12} /> 第 {source.page} 页 · 点击定位原文</span></button>)}</div></div>;
}
export function AnswerView({ result }: { result: Answer }) {
  const { openPaper } = useWorkspace();
  const audit = result.answer_audit;
  return <div className="answer-content"><div className={`mode-tag ${result.mode === 'extractive' ? 'muted' : ''}`}><span className="tiny-dot" />{result.mode === 'extractive' ? '原文检索摘录' : '基于文献生成'}</div>{result.warnings?.map((warning, i) => <Notice key={i}>{warning}</Notice>)}<Markdown text={result.answer} onEvidence={result.mode === 'model' ? index => { const source = result.sources[index]; if (source) openPaper(source.paper_id, source.page, source); } : undefined} />{audit?.status === 'fallback' && <details className="answer-audit"><summary>查看未通过检查的模型草稿</summary><p>以下是原模型草稿，保留供核对；请结合上方提示和原文判断。</p><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontFamily: 'inherit' }}>{audit.draft || audit.raw_response}</pre></details>}<SourceCards sources={result.sources} /></div>;
}
export function ModelHint() { const { health, setView } = useWorkspace(); if (health?.model_configured) return null; return <Notice>当前为检索摘录模式。配置模型后，可使用生成式问答、翻译和研究写作。<button className="text-button" onClick={() => setView('settings')}>配置模型 <ArrowUpRight size={13} /></button></Notice>; }
export function SelectionScope({ compact = false }: { compact?: boolean }) {
  const { papers, selected, setSelected } = useWorkspace();
  const available = papers.filter(p => ['ready', 'partial'].includes(p.status));
  return <div className={`scope ${compact ? 'compact' : ''}`}><div className="scope-title"><h3>本次研究文献</h3><span>{selected.length} 篇已选</span></div><p className="muted-text">选择一篇提问，或选择多篇进行比较。</p><div className="scope-actions"><button className="text-button" onClick={() => setSelected(available.map(p => p.id))}>全选可用文献</button><button className="text-button secondary-text" onClick={() => setSelected([])}>清空</button></div><div className="scope-list">{available.map(paper => <label className={`scope-item ${selected.includes(paper.id) ? 'selected' : ''}`} key={paper.id}><input type="checkbox" checked={selected.includes(paper.id)} onChange={() => setSelected(previous => previous.includes(paper.id) ? previous.filter(id => id !== paper.id) : [...previous, paper.id])} /><span><strong>{paper.title || paper.filename}</strong><small>{paper.page_count} 页{paper.version ? ` · v${paper.version}` : ''}</small></span>{selected.includes(paper.id) && <Check size={14} />}</label>)}{!available.length && <p className="muted-text inset">导入文献并完成解析后，即可在这里选择。</p>}</div></div>;
}
