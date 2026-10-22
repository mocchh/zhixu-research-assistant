import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowUp, BookOpen, CornerDownLeft, MessageSquare, Plus, RefreshCw, Sparkles, Square, Trash2, User } from 'lucide-react';
import { api, errorText, isAbort, post, type Answer } from './api';
import { useWorkspace } from './store';
import { AnswerView, ErrorBox, Modal, ModelHint, SelectionScope, Spinner } from './ui';
import { mergeMessages, saveMessages, type SavedMessage, type Session } from './persistence';
import { RequestScope } from './requestScope';

type UnsavedAnswer = { sessionId: string; message: SavedMessage };

export default function Chat() {
  const { selected, setSelected, papers, health, view } = useWorkspace();
  const [question, setQuestion] = useState('');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState<Session | null>(null);
  const [retryQuestion, setRetryQuestion] = useState<{ text: string; paperIds: string[] } | null>(null);
  const [lastRequestScope, setLastRequestScope] = useState<string[]>([]);
  const [unsavedAnswers, setUnsavedAnswers] = useState<UnsavedAnswer[]>([]);
  const unsaved = useRef(new Map<string, UnsavedAnswer>());
  // Keep local completed snapshots after a successful save too: an older GET
  // may still finish later and otherwise put the message back into pending.
  const completedAnswers = useRef(new Map<string, UnsavedAnswer>());
  const latestSessions = useRef(new Map<string, Session>());
  const deletedSessions = useRef(new Set<string>());
  const retrying = useRef(new Set<string>());
  const [savingAnswers, setSavingAnswers] = useState<string[]>([]);
  const activeId = useRef<string | null>(null);
  const generation = useRef(new RequestScope());
  const loadingRequest = useRef(new RequestScope());
  const pending = useRef<{ sessionId: string; message: SavedMessage } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const sessionsLoaded = useRef(false);
  const rememberAnswer = (value: UnsavedAnswer) => { completedAnswers.current.set(value.message.id, value); unsaved.current.set(value.message.id, value); setUnsavedAnswers([...unsaved.current.values()]); };
  const savedAnswers = (values: UnsavedAnswer[]) => { for (const value of values) if (unsaved.current.get(value.message.id) === value) unsaved.current.delete(value.message.id); setUnsavedAnswers([...unsaved.current.values()]); };
  const withLocalAnswers = (value: Session): Session => ({ ...value, messages: mergeMessages(value.messages, [...completedAnswers.current.values()].filter(item => item.sessionId === value.id && (unsaved.current.has(item.message.id) || !value.messages.some(message => message.id === item.message.id && message.status === 'completed'))).map(item => item.message)) });
  const refreshSessions = async () => setSessions((await api<{ sessions: Session[] }>('/sessions')).sessions);
  const applySession = (value: Session) => {
    if (deletedSessions.current.has(value.id)) return null;
    const known = latestSessions.current.get(value.id);
    const latest = known && known.revision > value.revision ? known : value;
    latestSessions.current.set(value.id, latest);
    if (activeId.current === value.id) setSession(withLocalAnswers(latest));
    return latest;
  };
  useEffect(() => { if (!unsavedAnswers.length) return; const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; }; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn); }, [unsavedAnswers.length]);
  const stop = async () => {
    generation.current.cancel(); setBusy(false);
    const current = pending.current; pending.current = null;
    if (!current) return;
    const stopped: SavedMessage = { ...current.message, status: 'error', content: '本次生成已停止。可保留问题后重试。' };
    if (activeId.current === current.sessionId) setSession(value => value ? { ...value, messages: mergeMessages(value.messages, [stopped]) } : value);
    try { applySession(await saveMessages(current.sessionId, [stopped])); await refreshSessions(); }
    catch (error) { if (activeId.current === current.sessionId) setError(`停止状态未保存：${errorText(error)}`); }
  };
  const openSession = async (id: string) => {
    void stop(); const operation = loadingRequest.current.begin(id);
    activeId.current = id; setLoading(true); setError(''); setRetryQuestion(null); setQuestion('');
    try {
      const value = await api<Session>(`/sessions/${id}`, { signal: operation.signal });
      if (operation.isCurrent()) { const latest = applySession(value); if (latest) setSelected(latest.paper_ids); }
    } catch (error) { if (operation.isCurrent() && !isAbort(error)) setError(errorText(error)); }
    finally { if (operation.isCurrent()) setLoading(false); }
  };
  useEffect(() => {
    if (view !== 'chat' || sessionsLoaded.current) return;
    sessionsLoaded.current = true;
    api<{ sessions: Session[] }>('/sessions').then(result => { setSessions(result.sessions); if (result.sessions[0] && !activeId.current) void openSession(result.sessions[0].id); }).catch(error => { sessionsLoaded.current = false; setError(errorText(error)); });
  }, [view]);
  useEffect(() => () => { generation.current.cancel(); loadingRequest.current.cancel(); }, []);
  useEffect(() => { if (view === 'chat' && session?.messages.length) end.current?.scrollIntoView({ block: 'nearest', behavior: 'instant' }); }, [session?.messages.length, busy, view]);
  const newSession = async () => {
    const operation = loadingRequest.current.begin('new'); setLoading(true); setError('');
    await stop(); if (!operation.isCurrent()) return;
    try { const value = await post<Session>('/sessions', { title: '新的研究对话', paper_ids: selected, messages: [] }, {signal:operation.signal}); if (!operation.isCurrent()) return; activeId.current = value.id; setSession(value); setQuestion(''); setRetryQuestion(null); await refreshSessions(); }
    catch (error) { if (operation.isCurrent() && !isAbort(error)) setError(errorText(error)); } finally { if(operation.isCurrent()) setLoading(false); }
  };
  const submit = async (event?: FormEvent, retry?: { text: string; paperIds: string[] }) => {
    event?.preventDefault();
    const text = (retry?.text || question).trim(); const scope = [...(retry?.paperIds || selected)];
    if (!text || !scope.length || busy || loading) return;
    const operation = generation.current.begin(activeId.current || 'new');
    setBusy(true); setError(''); setQuestion(''); setRetryQuestion({ text, paperIds: scope }); setLastRequestScope(scope);
    let currentSession = session;
    let assistant: SavedMessage | null = null;
    try {
      if (!currentSession) {
        currentSession = await post<Session>('/sessions', { title: text.slice(0, 35), paper_ids: scope, messages: [] }, { signal: operation.signal });
        if (!operation.isCurrent()) return;
        activeId.current = currentSession.id; setSession(currentSession);
      }
      const id = currentSession.id; const requestId = crypto.randomUUID();
      const user: SavedMessage = { id: crypto.randomUUID(), role: 'user', content: text, paper_ids: scope, sources: [], warnings: [], status: 'completed', request_id: requestId };
      assistant = { id: crypto.randomUUID(), role: 'assistant', content: '', paper_ids: scope, sources: [], warnings: [], status: 'pending', request_id: requestId };
      pending.current = { sessionId: id, message: assistant };
      const saved = await saveMessages(id, [user, assistant], scope);
      if (!operation.isCurrent() || activeId.current !== id) return;
      applySession(saved); void refreshSessions().catch(() => {});
      const history = currentSession.messages.filter(message => message.status === 'completed').slice(-8).map(message => ({ role: message.role, content: message.content.slice(0, 4000) }));
      const result = await post<Answer>('/chat', { question: text, paper_ids: scope, history }, { signal: operation.signal, timeoutMs: 180_000 });
      if (!operation.isCurrent() || activeId.current !== id) return;
      const completed: SavedMessage = { ...assistant, status: 'completed', content: result.answer, sources: result.sources, mode: result.mode, warnings: result.warnings, answer_audit: result.answer_audit };
      const retained = { sessionId: id, message: completed };
      rememberAnswer(retained);
      pending.current = null;
      setSession(value => value ? { ...value, messages: mergeMessages(value.messages, [completed]) } : value);
      try {
        const persisted = await saveMessages(id, [completed], scope);
        savedAnswers([retained]); applySession(persisted);
        if (operation.isCurrent() && activeId.current === id) setRetryQuestion(null);
      } catch (error) {
        if (operation.isCurrent()) { setRetryQuestion(null); setError(`回答已生成，但尚未保存：${errorText(error)}`); }
      }
      await refreshSessions().catch(() => {});
    } catch (error) {
      if (operation.isCurrent() && !isAbort(error)) {
        setError(errorText(error)); setQuestion(text);
        if (currentSession && assistant) {
          const failed: SavedMessage = { ...assistant, status: 'error', content: errorText(error) };
          pending.current = null;
          setSession(value => value ? { ...value, messages: mergeMessages(value.messages, [failed]) } : value);
          try { applySession(await saveMessages(currentSession.id, [failed])); } catch { setError(`${errorText(error)}；错误状态未能保存，请检查连接。`); }
        }
      }
    } finally { if (operation.isCurrent()) { setBusy(false); pending.current = null; } }
  };
  const deleteSession = async () => {
    if (!deleting) return;
    const id = deleting.id;
    if (activeId.current === id) { await stop(); loadingRequest.current.cancel(); }
    try { await api(`/sessions/${id}`, { method: 'DELETE' }); deletedSessions.current.add(id); latestSessions.current.delete(id); for (const [key, value] of completedAnswers.current) if (value.sessionId === id) { completedAnswers.current.delete(key); unsaved.current.delete(key); } setUnsavedAnswers([...unsaved.current.values()]); if (activeId.current === id) { activeId.current = null; setSession(null); } setDeleting(null); await refreshSessions(); }
    catch (error) { setError(errorText(error)); }
  };
  const retrySave = async () => {
    const id = activeId.current;
    if (!id || retrying.current.has(id)) return;
    const values = [...unsaved.current.values()].filter(value => value.sessionId === id);
    if (!values.length) return;
    retrying.current.add(id); setSavingAnswers([...retrying.current]);
    try { const value = await saveMessages(id, values.map(item => item.message)); savedAnswers(values); applySession(value); if (activeId.current === id) setError(''); await refreshSessions(); }
    catch(error) { if (activeId.current === id) setError(`保存仍未完成：${errorText(error)}`); }
    finally { retrying.current.delete(id); setSavingAnswers([...retrying.current]); }
  };
  const currentUnsaved = unsavedAnswers.filter(value => value.sessionId === session?.id);
  const examples = ['概括所选论文的核心贡献，并给出原文依据。', '对比这些论文使用的方法、数据和实验结果。', '这些论文有哪些局限？哪些结论需要谨慎理解？'];
  return <div className="page chat-page"><div className="page-heading"><div><div className="eyebrow">ASK. TRACE. UNDERSTAND.</div><h1>文献问答<span className="title-period">.</span></h1><p>对话与引用保存在本地，随时接着研究。</p></div><button className="button" disabled={loading} onClick={() => void newSession()}><Plus size={16}/>新对话</button></div><ModelHint/>
  <div className="chat-layout"><div className="chat-left"><section className="saved-list"><div className="saved-list-heading"><strong>历史会话</strong><button className="icon-button" onClick={() => void refreshSessions().catch(error => setError(errorText(error)))} aria-label="刷新会话"><RefreshCw size={14}/></button></div>{sessions.map(item => <div className={`saved-list-item ${item.id === session?.id ? 'active' : ''}`} key={item.id}><button onClick={() => void openSession(item.id)}>{item.title || '未命名对话'}{unsavedAnswers.some(value => value.sessionId === item.id) && <small> · 回答待保存</small>}</button><button className="icon-button" aria-label={`删除会话 ${item.title}`} onClick={() => setDeleting(item)}><Trash2 size={13}/></button></div>)}{!sessions.length && <p className="muted-text inset">提出问题后，自动保存新的会话。</p>}</section><SelectionScope/></div>
  <section className="chat-panel"><div className="chat-panel-heading"><div><MessageSquare size={17}/><strong>{session?.title || '与文献对话'}</strong></div><span><span className={`tiny-dot ${health?.model_configured ? 'green' : ''}`}/>{health?.model_configured ? health.model : '检索摘录模式'}</span></div>
  <div className="conversation">{loading ? <div className="loading-area"><Spinner label="正在读取已保存的会话…"/></div> : !session?.messages.length ? <div className="chat-welcome"><div className="chat-logo"><Sparkles size={29}/></div><span className="eyebrow">UNDERSTANDING STARTS WITH A QUESTION</span><h2>在论文之间，找到你的答案</h2><p>每次提问都会保存当时选中的文献与回答证据。</p><div className="suggested-questions">{examples.map((example, index) => <button key={example} onClick={() => { setQuestion(example); field.current?.focus(); }}><span>0{index + 1}</span><span>{example}</span><CornerDownLeft size={15}/></button>)}</div></div> : session.messages.map((message, index) => <article className={`message message-${message.role}`} key={message.id}><div className="message-avatar">{message.role === 'user' ? <User size={17}/> : <Sparkles size={17}/>}</div><div className="message-body"><div className="message-label">{message.role === 'user' ? '你的问题' : '知序 · 研究助手'}<span className="message-scope" title={message.paper_ids.map(id => papers.find(p => p.id === id)?.title || id).join('\n')}>本次 {message.paper_ids.length} 篇 · 范围已保存</span></div>{message.role === 'assistant' && message.status === 'completed' ? <AnswerView result={{ answer: message.content, sources: message.sources, mode: message.mode || 'extractive', warnings: message.warnings, answer_audit: message.answer_audit }}/>: message.status === 'pending' && busy && pending.current?.message.id === message.id ? <Spinner label="正在整理证据…"/> : (message.status === 'error' || message.status === 'pending') ? <div><ErrorBox>{message.status === 'pending' ? '该请求尚未记录完成；此窗口不会自动续接，可重试问题。' : message.content}</ErrorBox><button className="button small" disabled={busy} onClick={() => { const prompt = session.messages.slice(0, index).reverse().find(item => item.role === 'user'); if (prompt) void submit(undefined, { text: prompt.content, paperIds: prompt.paper_ids }); }}><RefreshCw size={13}/>重试这次问题</button></div> : <p>{message.content}</p>}</div></article>)}{error && <div className="inset"><ErrorBox>{error}</ErrorBox>{retryQuestion && !busy && <button className="button small" onClick={() => void submit(undefined, retryQuestion)}>重试未完成的请求</button>}</div>}{!!currentUnsaved.length && <div className="inset notice"><div><p>{currentUnsaved.length} 条回答尚未保存到数据库；切换会话仍可返回查看，请在关闭页面前保存。</p><button className="button small" disabled={busy || savingAnswers.includes(session?.id || '')} onClick={() => void retrySave()}>{savingAnswers.includes(session?.id || '') ? '正在保存回答…' : '重试保存回答'}</button></div></div>}<div ref={end}/></div>
  <form className="composer" onSubmit={event => void submit(event)}><textarea maxLength={4000} ref={field} value={question} onChange={event => setQuestion(event.target.value)} placeholder={selected.length ? `向 ${selected.length} 篇所选文献提问…` : '请先选择至少一篇已解析文献…'} aria-label="输入文献问题" rows={3} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }}/><div className="composer-footer"><span><BookOpen size={13}/>{busy ? lastRequestScope.length : selected.length} 篇文献<span className="composer-shortcut">· Enter 发送，Shift + Enter 换行</span></span>{busy ? <button type="button" className="button small" onClick={() => void stop()}><Square size={13}/>停止</button> : <button className="send-button" disabled={loading || !question.trim() || !selected.length} aria-label="发送问题"><ArrowUp size={19}/></button>}</div></form><div className="chat-disclaimer">回答与引用保存在本地数据库；请核查原文。</div></section></div>
  {deleting && <Modal title="删除已保存的会话？" close={() => setDeleting(null)}><p className="modal-description">将删除“{deleting.title}”及其全部消息，此操作无法撤销。</p><div className="modal-footer"><button className="button" onClick={() => setDeleting(null)}>取消</button><button className="button danger" onClick={() => void deleteSession()}>确认删除</button></div></Modal>}</div>;
}
