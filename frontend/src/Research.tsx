import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowDownToLine, BookOpen, Braces, CheckCircle2, Copy, FileText, FlaskConical, Plus, Save, Sparkles, Square, Trash2 } from 'lucide-react';
import { api, errorText, isAbort, post, type Answer, type Evidence } from './api';
import { useWorkspace } from './store';
import { downloadApiFile, downloadText, type Note } from './persistence';
import { RequestScope } from './requestScope';
import { ErrorBox, Markdown, Modal, ModelHint, Notice, SelectionScope, SourceCards, Spinner } from './ui';
import Charts from './Charts';

type NoteBody = Pick<Note, 'title' | 'content' | 'sources' | 'paper_ids' | 'kind'>;

export function noteMarkdown(title: string, content: string, sources: Evidence[], baseUrl: string) {
  return `# ${title}\n\n${content}\n\n## 引用证据\n\n${sources.map((source, i) => {
    const meta = source as Evidence & { authors?: string[]; arxiv_id?: string; filename?: string; version?: number; year?: number; doi?: string };
    const metadata = [meta.authors?.join('；'), meta.year, meta.arxiv_id && `arXiv:${meta.arxiv_id}`, meta.version && `v${meta.version}`, meta.doi && `DOI:${meta.doi}`, meta.filename].filter(Boolean).join(' · ');
    return `[${i + 1}] **${source.paper_title}**\n\n${metadata ? `${metadata}\n\n` : ''}[原文第 ${source.page} 页](${baseUrl}/api/papers/${encodeURIComponent(source.paper_id)}/file#page=${source.page})\n\n${source.text.split('\n').map(line => `> ${line}`).join('\n')}\n\n定位：paper_id=${source.paper_id}；block_id=${source.id}；bbox=${JSON.stringify(source.bbox)}`;
  }).join('\n\n')}`;
}
export default function Research() {
  const { selected, setSelected, health, notify, view } = useWorkspace();
  const [task, setTask] = useState<'summary' | 'review' | 'ideas' | 'outline'>('review');
  const [idea, setIdea] = useState('');
  const [notes, setNotes] = useState<Note[]>([]);
  const [note, setNote] = useState<Note | null>(null);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [sources, setSources] = useState<Evidence[]>([]);
  const [scope, setScope] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [deleteNote, setDeleteNote] = useState<Note | null>(null);
  const [leaveDraft, setLeaveDraft] = useState<{ label: string; run: () => void } | null>(null);
  const [generatedDraft, setGeneratedDraft] = useState<NoteBody | null>(null);
  const operation = useRef(new RequestScope());
  const editorRevision = useRef(0);
  const saveInProgress = useRef(false);
  const opened = useRef(false);
  const dirty = note ? title !== note.title || content !== note.content || JSON.stringify(sources) !== JSON.stringify(note.sources) || JSON.stringify(scope) !== JSON.stringify(note.paper_ids) : Boolean(title.trim() || content.trim());
  useEffect(() => { if (!dirty && !generatedDraft) return; const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; }; window.addEventListener('beforeunload',warn); return () => window.removeEventListener('beforeunload',warn); }, [dirty, generatedDraft]);
  const tasks = [{ id: 'summary' as const, name: '核心摘要', icon: FileText, description: '提炼研究问题、方法与结论' }, { id: 'review' as const, name: '文献综述', icon: BookOpen, description: '组织多篇文献的关系' }, { id: 'ideas' as const, name: '研究方向', icon: FlaskConical, description: '从局限和未来工作寻找线索' }, { id: 'outline' as const, name: '写作提纲', icon: Braces, description: '结合研究想法组织结构' }];
  const refreshNotes = async () => setNotes((await api<{ notes: Note[] }>('/notes')).notes);
  const apply = (value: Note) => { editorRevision.current++; setNote(value); setTitle(value.title); setContent(value.content); setSources(value.sources); setScope(value.paper_ids); setSelected(value.paper_ids); setWarnings([]); setEditing(false); };
  const protectDraft = (label: string, run: () => void) => { if (dirty) setLeaveDraft({ label, run }); else run(); };
  const newNote = () => { operation.current.cancel(); editorRevision.current++; setBusy(false); setNote(null); setTitle(''); setContent(''); setSources([]); setScope(selected); setWarnings([]); setError(''); setEditing(true); };
  const open = async (id: string) => {
    const current = operation.current.begin(id); const revision = editorRevision.current; setBusy(false); setError('');
    try { const value = await api<Note>(`/notes/${id}`, { signal: current.signal }); if (current.isCurrent()) { if (editorRevision.current === revision) apply(value); else setError('读取期间有新的编辑，已保留当前草稿；请保存后再打开笔记。'); } }
    catch (error) { if (current.isCurrent() && !isAbort(error)) setError(errorText(error)); }
  };
  useEffect(() => { if (view !== 'research' || opened.current) return; opened.current = true; const revision = editorRevision.current; api<{notes: Note[]}>('/notes').then(result => { setNotes(result.notes); if (result.notes[0] && editorRevision.current === revision) void open(result.notes[0].id); }).catch(error => { opened.current = false; setError(errorText(error)); }); }, [view]);
  useEffect(() => () => operation.current.cancel(), []);
  const save = async (body?: { title: string; content: string; sources: Evidence[]; paper_ids: string[]; kind: Note['kind'] }) => {
    const data = body || { title: title.trim() || '研究笔记', content, sources, paper_ids: scope.length ? scope : selected, kind: note?.kind || 'note' as const };
    if (!data.content.trim() || saveInProgress.current || busy) return;
    operation.current.cancel();
    const revision = editorRevision.current;
    saveInProgress.current = true; setSaving(true); setError('');
    try {
      const value = body || !note ? await post<Note>('/notes', data) : await api<Note>(`/notes/${note.id}`, { method: 'PUT', body: JSON.stringify({ ...data, revision: note.revision }) });
      // The write response is the saved baseline. A subsequent GET could already
      // contain another window's edits, which must not become our own revision.
      if (editorRevision.current === revision) { apply(value); notify('研究笔记已保存到本地数据库。'); }
      else { setNote(value); notify('已保存提交时的内容；之后的编辑仍保留，请再次保存。'); }
      await refreshNotes();
    } catch (error) { setError(`${errorText(error)} 当前内容仍保留在编辑区；版本冲突时可另存为新笔记。`); }
    finally { saveInProgress.current = false; setSaving(false); }
  };
  const generate = async (event: FormEvent) => {
    event.preventDefault(); if (!selected.length || busy || saving || generatedDraft) return;
    const current = operation.current.begin('generate'); const paperIds = [...selected]; const currentTask = task;
    const revision = editorRevision.current; const keepDraft = dirty;
    setBusy(true); setError('');
    try {
      const result = await post<Answer>('/research', { task: currentTask, paper_ids: paperIds, idea: idea.trim() }, { signal: current.signal, timeoutMs: 180_000 });
      if (!current.isCurrent()) return;
      const nextTitle = `${tasks.find(item => item.id === currentTask)?.name || '研究笔记'} · ${new Date().toLocaleDateString('zh-CN')}`;
      setSaving(true);
      const data = { title: nextTitle, content: result.answer, sources: result.sources, paper_ids: paperIds, kind: currentTask };
      setGeneratedDraft(data);
      // Generating a new result must not replace a draft, including edits made
      // while waiting for the model or the storage response.
      const value = await post<Note>('/notes', data);
      setGeneratedDraft(pending => pending === data ? null : pending);
      if (current.isCurrent()) {
        if (!keepDraft && editorRevision.current === revision) { apply(value); setWarnings(result.warnings); }
        else notify('新结果已另存为笔记；当前草稿保留，可从笔记列表打开新结果。');
        await refreshNotes();
      }
    } catch (error) { if (current.isCurrent() && !isAbort(error)) setError(`${errorText(error)} 上一次结果及当前内容仍保留。`); }
    finally { if (current.isCurrent()) { setBusy(false); setSaving(false); } }
  };
  const saveGenerated = async () => {
    if (!generatedDraft || saving) return; setSaving(true); setError('');
    try { await post<Note>('/notes', generatedDraft); setGeneratedDraft(null); await refreshNotes(); notify('生成结果已另存为笔记，当前编辑内容保留。'); }
    catch (error) { setError(`生成结果仍未保存：${errorText(error)}`); }
    finally { setSaving(false); }
  };
  const restore = async (revision: number) => {
    if (!note || saving) return;
    const current = operation.current.begin(`restore:${note.id}:${revision}`); const edit = editorRevision.current; setBusy(false); setError('');
    try { const value = await api<Note>(`/notes/${note.id}/versions/${revision}`, {signal: current.signal}); if (!current.isCurrent()) return;
      if (editorRevision.current !== edit) { setError('读取期间有新的编辑，已保留当前草稿；请保存后再恢复版本。'); return; }
      editorRevision.current++; setTitle(value.title); setContent(value.content); setSources(value.sources); setScope(value.paper_ids); setEditing(true); notify('历史版本已载入编辑区；保存后将创建新版本。');
    } catch (error) { if (current.isCurrent() && !isAbort(error)) setError(errorText(error)); }
  };
  const remove = async () => { if (!deleteNote) return; try { await api(`/notes/${deleteNote.id}`, { method: 'DELETE' }); if (note?.id === deleteNote.id) { setNote(null); setContent(''); setSources([]); setTitle(''); } setDeleteNote(null); await refreshNotes(); } catch (error) { setError(errorText(error)); } };
  return <div className="page research-page"><div className="page-heading"><div><div className="eyebrow">FROM EVIDENCE TO IDEAS</div><h1>研究写作<span className="title-period">.</span></h1><p>研究笔记与历史版本保存在本地，保留每一步思考。</p></div><button className="button" disabled={saving} onClick={() => protectDraft('新建笔记', newNote)}><Plus size={15}/>新笔记</button></div><ModelHint/>
  <div className="research-layout"><div><section className="saved-list"><div className="saved-list-heading"><strong>研究笔记</strong><span>{notes.length}</span></div>{notes.map(item => <div className={`saved-list-item ${item.id === note?.id ? 'active' : ''}`} key={item.id}><button disabled={saving} onClick={() => protectDraft('打开另一篇笔记', () => void open(item.id))}>{item.title}</button><button className="icon-button" disabled={saving} aria-label={`删除笔记 ${item.title}`} onClick={() => setDeleteNote(item)}><Trash2 size={13}/></button></div>)}{!notes.length && <p className="muted-text inset">生成内容后自动保存，也可新建笔记。</p>}</section><SelectionScope/></div>
  <div className="research-main"><form className="research-form" onSubmit={event => void generate(event)}><h2>今天，想推进哪一步？</h2><div className="research-tasks">{tasks.map(item => <button className={task === item.id ? 'active' : ''} type="button" key={item.id} onClick={() => setTask(item.id)}><item.icon size={20}/><strong>{item.name}</strong><span>{item.description}</span>{task === item.id && <CheckCircle2 size={16} className="task-check"/>}</button>)}</div><label className="form-label" htmlFor="research-idea">{task === 'outline' ? '你的研究想法 · 必填' : '想聚焦的问题 · 选填'}</label><textarea id="research-idea" rows={4} maxLength={6000} required={task === 'outline'} value={idea} onChange={event => setIdea(event.target.value)} placeholder="描述研究问题、方法思路或希望比较的维度…"/>{task === 'ideas' && <Notice>候选方向来自当前文献，需要检索最新工作后再判断新颖性。</Notice>}<div className="research-form-footer"><span>{selected.length} 篇文献作为依据</span>{busy ? <button type="button" className="button" disabled={saving} onClick={() => { operation.current.cancel(); setBusy(false); }}><Square size={14}/>停止生成</button> : <button className="button primary" disabled={saving || !!generatedDraft || !selected.length || (task === 'outline' && !idea.trim())}><Sparkles size={15}/>{health?.model_configured ? '生成并保存笔记' : '整理并保存摘录'}</button>}</div></form>
  {error && <ErrorBox>{error}</ErrorBox>}{generatedDraft && !busy && <section className="notice"><div><p>生成结果尚未保存，当前草稿仍保留。重试保存不会再次调用模型。</p><button className="button small" disabled={saving} onClick={() => void saveGenerated()}>重试保存生成结果</button><details><summary>查看未保存的生成结果</summary><Markdown text={generatedDraft.content}/></details></div></section>}{busy && <div className="research-working"><Spinner label="正在整理证据；上次结果保持可用…"/></div>}
  {(content || editing) && <section className="research-result"><div className="research-result-heading"><h2>{note ? `${dirty ? '未保存修改' : '已保存'} · 版本 ${note.revision}` : '尚未保存的笔记'}</h2><div><button className="button small" onClick={() => setEditing(!editing)}>{editing ? '预览' : '编辑'}</button><button className="button small primary" disabled={saving || busy || !content.trim()} onClick={() => void save()}><Save size={13}/>{saving ? '保存中' : '保存版本'}</button></div></div><input className="form-input" aria-label="研究笔记标题" maxLength={500} value={title} onChange={event => { editorRevision.current++; setTitle(event.target.value); }} placeholder="笔记标题"/>{note?.versions && note.versions.length > 1 && <label className="note-version">历史版本<select aria-label="查看笔记历史版本" disabled={saving} defaultValue="" onChange={event => { if (event.target.value) { const revision = Number(event.target.value); protectDraft('恢复历史版本', () => void restore(revision)); } event.target.value = ''; }}><option value="">选择版本恢复到编辑区</option>{note.versions.map(version => <option key={version.revision} value={version.revision}>版本 {version.revision} · {version.created_at}</option>)}</select></label>}{warnings.map((warning, i) => <Notice key={i}>{warning}</Notice>)}{editing ? <textarea className="note-editor" aria-label="研究笔记正文" maxLength={500000} value={content} onChange={event => { editorRevision.current++; setContent(event.target.value); }} rows={15}/> : <div className="note-preview"><Markdown text={content}/></div>}<SourceCards sources={sources}/><div className="note-export"><button className="button small" onClick={() => void navigator.clipboard.writeText(content).then(() => notify('已复制笔记。')).catch(() => notify('复制失败，请手动复制。', 'error'))}><Copy size={13}/>复制</button><button className="button small" onClick={() => downloadText(noteMarkdown(title || '研究笔记', content, sources, window.location.origin), `${title || '研究笔记'}.md`)}><ArrowDownToLine size={13}/>完整 Markdown</button>{scope.length > 0 && <button className="button small" onClick={() => void downloadApiFile(`/citations/export?paper_ids=${encodeURIComponent(scope.join(','))}`, `${title || '文献'}.bib`).catch(error => notify(errorText(error), 'error'))}>BibTeX</button>}<button className="button small" disabled={saving || busy || !content.trim()} onClick={() => void save({ title: `${title || '研究笔记'}（副本）`, content, sources, paper_ids: scope, kind: note?.kind || 'note' })}>另存为新笔记</button></div></section>}<Charts/></div></div>
  {leaveDraft && <Modal title="保留当前草稿？" close={() => setLeaveDraft(null)}><p className="modal-description">当前有未保存修改。可先返回保存；继续{leaveDraft.label}将放弃这些修改。</p><div className="modal-footer"><button className="button" onClick={() => setLeaveDraft(null)}>返回保留草稿</button><button className="button danger" onClick={() => { const action = leaveDraft.run; setLeaveDraft(null); action(); }}>放弃修改并继续</button></div></Modal>}{deleteNote && <Modal title="删除研究笔记？" close={() => setDeleteNote(null)}><p className="modal-description">将删除“{deleteNote.title}”及历史版本。</p><div className="modal-footer"><button className="button" onClick={() => setDeleteNote(null)}>取消</button><button className="button danger" onClick={() => void remove()}>确认删除</button></div></Modal>}</div>;
}
