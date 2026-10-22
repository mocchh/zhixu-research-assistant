// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Note, Session } from './persistence';

const state = vi.hoisted(() => ({
  view: 'research', notes: {} as Record<string, Note>, sessions: {} as Record<string, Session>,
  failAnswers: false, failGenerated: false, noteCounter: 0, modelCalls: 0, researchCalls: 0,
  apiHook: null as null | ((path: string, options: RequestInit) => Promise<unknown> | undefined),
  researchHook: null as null | (() => Promise<unknown>),
  chatHook: null as null | ((question: string) => Promise<unknown> | undefined),
  notify: vi.fn(), selected: ['p'],
}));
vi.mock('./store', () => ({ useWorkspace: () => ({ view: state.view, selected: state.selected, setSelected: vi.fn(), papers: [], health: { model_configured: true }, notify: state.notify }) }));
vi.mock('./Charts', () => ({ default: () => null }));
vi.mock('./ui', () => ({
  AnswerView: ({ result }: { result: { answer: string; answer_audit?: unknown; sources: unknown[] } }) => <div><p>{result.answer}</p><pre data-testid="answer-audit">{JSON.stringify(result.answer_audit)}</pre><pre data-testid="answer-sources">{JSON.stringify(result.sources)}</pre></div>,
  ErrorBox: ({ children }: { children: React.ReactNode }) => <div role="alert">{children}</div>,
  Modal: ({ title, children }: { title: string; children: React.ReactNode }) => <div role="dialog" aria-label={title}>{children}</div>,
  Markdown: ({ text }: { text: string }) => <p>{text}</p>,
  Notice: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Spinner: ({ label }: { label: string }) => <span>{label}</span>,
  ModelHint: () => null, SelectionScope: () => null, SourceCards: () => null,
}));
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>();
  const clone = <T,>(value: T): T => structuredClone(value);
  return { ...original,
    api: vi.fn(async (path: string, options: RequestInit = {}) => {
      const override = state.apiHook?.(path, options); if (override) return override;
      if (path === '/notes') return { notes: clone(Object.values(state.notes)) };
      if (path.startsWith('/notes/')) {
        const [, , id, versions] = path.split('/'); const current = state.notes[id];
        if (versions) return { ...clone(current), content: 'Historical text', sources: [], paper_ids: ['historical-p'] };
        if (options.method === 'PUT') {
          const body = JSON.parse(options.body as string);
          if (body.revision !== current.revision) throw new original.HttpError('conflict', 409);
          state.notes[id] = { ...current, ...body, revision: current.revision + 1 };
        }
        return clone(state.notes[id]);
      }
      if (path === '/sessions') return { sessions: clone(Object.values(state.sessions)) };
      if (path.startsWith('/sessions/')) {
        const id = path.split('/')[2];
        if (options.method === 'DELETE') { delete state.sessions[id]; return {}; }
        if (options.method === 'PUT') {
          const body = JSON.parse(options.body as string);
          if (state.failAnswers && body.messages.some((m: { role: string; status: string }) => m.role === 'assistant' && m.status === 'completed')) throw new Error('Save offline');
          state.sessions[id] = { ...state.sessions[id], ...body, revision: body.revision + 1 };
        }
        return clone(state.sessions[id]);
      }
      throw new Error(`Unexpected API ${path}`);
    }),
    post: vi.fn(async (path: string, body: Record<string, unknown>) => {
      if (path === '/notes') {
        if (state.failGenerated) throw new Error('Note offline');
        const id = `generated-${++state.noteCounter}`;
        const value: Note = { title: String(body.title), content: String(body.content), sources: body.sources as Note['sources'], paper_ids: body.paper_ids as string[], kind: body.kind as Note['kind'], id, revision: 1, created_at: '', updated_at: '', versions: [] };
        state.notes[id] = value; return clone(value);
      }
      if (path === '/research') {
        state.researchCalls++; return state.researchHook ? state.researchHook() : { answer: 'Generated result', sources: [], warnings: [], mode: 'model' };
      }
      if (path === '/chat') {
        state.modelCalls++;
        const override = state.chatHook?.(String(body.question)); if (override) return override;
        return { answer: `Answer ${body.question}`, sources: [{ id: 'block', text: 'real evidence', paper_id: 'p', page: 7, bbox: [1, 2, 3, 4] }], mode: 'extractive', warnings: ['review'], answer_audit: { status: 'fallback', raw_response: 'raw draft', draft: 'draft text', semantic_entailment_verified: false, issues: ['issue'], citation_checks: {} } };
      }
      throw new Error(`Unexpected POST ${path}`);
    }),
  };
});
import Research from './Research';
import Chat from './Chat';
import { api } from './api';

function deferred<T = unknown>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const note = (id: string): Note => ({ id, title: `Note ${id}`, content: `Original ${id}`, sources: [], paper_ids: ['p'], kind: 'note', revision: 2, created_at: '', updated_at: '', versions: [{ revision: 1, title: `Note ${id}`, created_at: '' }, { revision: 2, title: `Note ${id}`, created_at: '' }] });
const session = (id: string): Session => ({ id, title: `Session ${id}`, messages: [], paper_ids: ['p'], revision: 1, created_at: '', updated_at: '' });
const body = () => screen.getByLabelText('研究笔记正文') as HTMLTextAreaElement;
const changeBody = (value: string) => fireEvent.change(body(), { target: { value } });
async function readyNote() { render(<Research/>); await screen.findByDisplayValue('Note a'); fireEvent.click(screen.getByRole('button', { name: '编辑' })); }
async function ask(question: string) { fireEvent.change(screen.getByLabelText('输入文献问题'), { target: { value: question } }); fireEvent.click(screen.getByLabelText('发送问题')); await screen.findByText(`Answer ${question}`); await waitFor(() => expect(screen.queryByRole('button', { name: '停止' })).toBeNull()); }
beforeEach(() => { state.view = 'research'; state.notes = { a: note('a'), b: note('b') }; state.sessions = { a: session('a'), b: session('b') }; state.failAnswers = false; state.failGenerated = false; state.noteCounter = 0; state.modelCalls = 0; state.researchCalls = 0; state.apiHook = null; state.researchHook = null; state.chatHook = null; state.notify.mockClear(); vi.mocked(api).mockClear(); HTMLElement.prototype.scrollIntoView = vi.fn(); });
afterEach(cleanup);

describe('note draft protection', () => {
  it('requires an explicit discard for new notes and keeps the draft when cancelled', async () => {
    await readyNote(); changeBody('Unsent draft'); fireEvent.click(screen.getByRole('button', { name: '新笔记' }));
    expect(screen.getByRole('dialog')).toBeTruthy(); fireEvent.click(screen.getByText('返回保留草稿')); expect(body().value).toBe('Unsent draft');
    fireEvent.click(screen.getByRole('button', { name: '新笔记' })); fireEvent.click(screen.getByText('放弃修改并继续')); expect(body().value).toBe('');
  });
  it('guards switching notes and restoring history, but opens clean notes directly', async () => {
    await readyNote(); changeBody('Local draft'); fireEvent.click(screen.getByRole('button', { name: 'Note b' }));
    fireEvent.click(screen.getByText('返回保留草稿')); expect(body().value).toBe('Local draft');
    fireEvent.change(screen.getByLabelText('查看笔记历史版本'), { target: { value: '1' } });
    expect(screen.getByRole('dialog')).toBeTruthy(); fireEvent.click(screen.getByText('放弃修改并继续'));
    await waitFor(() => expect(body().value).toBe('Historical text'));
    fireEvent.click(screen.getByRole('button', { name: 'Note b' })); fireEvent.click(screen.getByText('放弃修改并继续'));
    await screen.findByDisplayValue('Note b'); fireEvent.click(screen.getByRole('button', { name: 'Note a' }));
    await screen.findByDisplayValue('Note a'); expect(screen.queryByRole('dialog')).toBeNull();
  });
  it('keeps edits typed during a save and uses the saved revision on the next save', async () => {
    await readyNote(); changeBody('Snapshot sent'); const gate = deferred<Note>();
    state.apiHook = (path, options) => { if (path === '/notes/a' && options.method === 'PUT') { const data = JSON.parse(options.body as string); state.notes.a = { ...state.notes.a, ...data, revision: data.revision + 1 }; return gate.promise; } };
    fireEvent.click(screen.getByText('保存版本')); await waitFor(() => expect(state.notes.a.content).toBe('Snapshot sent'));
    changeBody('Typed after sending'); fireEvent.change(screen.getByLabelText('研究笔记标题'), { target: { value: 'New title while saving' } });
    await act(async () => gate.resolve(structuredClone(state.notes.a)));
    expect(body().value).toBe('Typed after sending'); expect((screen.getByLabelText('研究笔记标题') as HTMLInputElement).value).toBe('New title while saving');
    state.apiHook = null; fireEvent.click(screen.getByText('保存版本')); await waitFor(() => expect(state.notes.a.revision).toBe(4));
    expect(state.notes.a.content).toBe('Typed after sending');
  });
  it('ignores an old history response after opening a different note', async () => {
    await readyNote(); const gate = deferred<Note>(); state.apiHook = path => path.includes('/versions/') ? gate.promise : undefined;
    fireEvent.change(screen.getByLabelText('查看笔记历史版本'), { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Note b' })); await screen.findByDisplayValue('Note b');
    await act(async () => gate.resolve({ ...note('a'), content: 'LATE HISTORY' })); expect(screen.queryByText('LATE HISTORY')).toBeNull(); expect(screen.getByText('Original b')).toBeTruthy();
  });
  it('does not replace text edited while another note is loading', async () => {
    await readyNote(); const gate = deferred<Note>(); state.apiHook = path => path === '/notes/b' ? gate.promise : undefined;
    fireEvent.click(screen.getByRole('button', { name: 'Note b' })); changeBody('Edited while loading');
    await act(async () => gate.resolve(note('b'))); expect(body().value).toBe('Edited while loading'); expect(screen.getByRole('alert').textContent).toContain('已保留当前草稿');
  });
  it('saves generated results separately when a draft already exists', async () => {
    await readyNote(); changeBody('Keep my work'); fireEvent.click(screen.getByText('生成并保存笔记'));
    await waitFor(() => expect(state.notes['generated-1']?.content).toBe('Generated result'));
    expect(body().value).toBe('Keep my work'); expect(state.notes.a.content).toBe('Original a'); expect(state.researchCalls).toBe(1);
  });
  it('preserves typing that starts while generation is running', async () => {
    await readyNote(); const gate = deferred(); state.researchHook = () => gate.promise;
    fireEvent.click(screen.getByText('生成并保存笔记')); changeBody('New edits during generation');
    await act(async () => gate.resolve({ answer: 'Generated late', sources: [], warnings: [] }));
    await waitFor(() => expect(state.notes['generated-1']?.content).toBe('Generated late')); expect(body().value).toBe('New edits during generation');
  });
  it('retains an unsaved generated result and retries storage without another model call', async () => {
    await readyNote(); changeBody('Local work'); state.failGenerated = true; fireEvent.click(screen.getByText('生成并保存笔记'));
    await screen.findByText('重试保存生成结果'); expect(screen.getByText('Generated result')).toBeTruthy(); expect(body().value).toBe('Local work');
    state.failGenerated = false; fireEvent.click(screen.getByText('重试保存生成结果'));
    await waitFor(() => expect(state.notes['generated-1']?.content).toBe('Generated result')); expect(state.researchCalls).toBe(1); expect(body().value).toBe('Local work');
  });
});

describe('unsaved chat answers', () => {
  it('ignores an older save response after a newer question is already pending', async () => {
    state.view = 'chat'; const saveGate = deferred<Session>(); const answerGate = deferred(); let saved: Session | undefined;
    state.apiHook = (path, options) => {
      if (!saved && path === '/sessions/a' && options.method === 'PUT') {
        const data = JSON.parse(options.body as string);
        if (data.messages.some((m: { role: string; status: string }) => m.role === 'assistant' && m.status === 'completed')) {
          saved = { ...state.sessions.a, ...data, revision: data.revision + 1 }; state.sessions.a = saved!; return saveGate.promise;
        }
      }
    };
    state.chatHook = question => question === 'second pending question' ? answerGate.promise : undefined;
    render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' });
    fireEvent.change(screen.getByLabelText('输入文献问题'), { target: { value: 'first' } }); fireEvent.click(screen.getByLabelText('发送问题')); await screen.findByText('Answer first');
    fireEvent.click(screen.getByRole('button', { name: '停止' }));
    fireEvent.change(screen.getByLabelText('输入文献问题'), { target: { value: 'second pending question' } }); fireEvent.click(screen.getByLabelText('发送问题'));
    await waitFor(() => expect(state.modelCalls).toBe(2)); await act(async () => saveGate.resolve(saved!));
    expect(screen.getByText('second pending question')).toBeTruthy(); expect(screen.getByText('正在整理证据…')).toBeTruthy();
    await act(async () => answerGate.resolve({ answer: 'Second completed', sources: [], mode: 'extractive', warnings: [] }));
    await screen.findByText('Second completed'); expect(state.sessions.a.messages).toHaveLength(4);
  });
  it('reopens a failed save with its full evidence/audit and retries storage only', async () => {
    state.view = 'chat'; state.failAnswers = true; render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' }); await ask('one');
    const audit = screen.getByTestId('answer-audit').textContent; const sources = screen.getByTestId('answer-sources').textContent;
    fireEvent.click(screen.getByRole('button', { name: /^Session a/ })); await screen.findByText('Answer one');
    expect(screen.getByTestId('answer-audit').textContent).toBe(audit); expect(screen.getByTestId('answer-sources').textContent).toBe(sources); expect(screen.getByText('重试保存回答')).toBeTruthy();
    state.failAnswers = false; fireEvent.click(screen.getByText('重试保存回答'));
    await waitFor(() => expect(state.sessions.a.messages.at(-1)?.status).toBe('completed')); expect(state.modelCalls).toBe(1);
    expect(state.sessions.a.messages.at(-1)?.answer_audit?.raw_response).toBe('raw draft');
  });
  it('keeps independent unsaved answers in two sessions and does not clear the other on success', async () => {
    state.view = 'chat'; state.failAnswers = true; render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' }); await ask('A');
    fireEvent.click(screen.getByRole('button', { name: /^Session b/ })); await screen.findByText('Session b', { selector: 'strong' }); await ask('B');
    state.failAnswers = false; fireEvent.click(screen.getByText('重试保存回答')); await waitFor(() => expect(state.sessions.b.messages.at(-1)?.status).toBe('completed'));
    fireEvent.click(screen.getByRole('button', { name: /^Session a/ })); await screen.findByText('Answer A'); expect(screen.getByText('重试保存回答')).toBeTruthy();
    fireEvent.click(screen.getByText('重试保存回答')); await waitFor(() => expect(state.sessions.a.messages.at(-1)?.status).toBe('completed')); expect(state.modelCalls).toBe(2);
  });
  it('retains a failed answer even when its save fails only after switching sessions', async () => {
    state.view = 'chat'; const gate = deferred();
    state.apiHook = (path, options) => path === '/sessions/a' && options.method === 'PUT' && JSON.parse(options.body as string).messages.some((m: { role: string; status: string }) => m.role === 'assistant' && m.status === 'completed') ? gate.promise.then(() => { throw new Error('Late failure'); }) : undefined;
    render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' });
    fireEvent.change(screen.getByLabelText('输入文献问题'), { target: { value: 'late' } }); fireEvent.click(screen.getByLabelText('发送问题')); await screen.findByText('Answer late');
    fireEvent.click(screen.getByRole('button', { name: /^Session b/ })); await screen.findByText('Session b', { selector: 'strong' }); await act(async () => gate.resolve(undefined));
    fireEvent.click(screen.getByRole('button', { name: /^Session a/ })); await screen.findByText('Answer late'); expect(screen.getByText('重试保存回答')).toBeTruthy(); expect(state.modelCalls).toBe(1);
  });
  it('does not lose an earlier unsaved answer when a subsequent answer saves successfully', async () => {
    state.view = 'chat'; state.failAnswers = true; render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' }); await ask('first');
    state.failAnswers = false; await ask('second');
    expect(screen.getByText('Answer first')).toBeTruthy(); expect(screen.getByText('Answer second')).toBeTruthy(); expect(screen.getByText('重试保存回答')).toBeTruthy();
    const unload = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(unload); expect(unload.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByText('重试保存回答'));
    await waitFor(() => expect(state.sessions.a.messages.filter(m => m.role === 'assistant').every(m => m.status === 'completed')).toBe(true));
    expect(state.modelCalls).toBe(2);
  });
  it('does not downgrade a saved answer when an earlier session load finishes late', async () => {
    state.view = 'chat'; state.failAnswers = true; render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' }); await ask('snapshot');
    const stale = structuredClone(state.sessions.a); const gate = deferred<Session>(); let loading = false;
    state.apiHook = (path, options) => { if (path === '/sessions/a' && !options.method && !loading) { loading = true; return gate.promise; } };
    fireEvent.click(screen.getByRole('button', { name: /^Session a/ })); await waitFor(() => expect(loading).toBe(true));
    state.failAnswers = false; fireEvent.click(screen.getByText('重试保存回答'));
    await waitFor(() => expect(state.sessions.a.messages.at(-1)?.status).toBe('completed'));
    await act(async () => gate.resolve(stale)); expect(screen.getByText('Answer snapshot')).toBeTruthy(); expect(screen.queryByText('重试这次问题')).toBeNull();
  });
  it('does not resurrect a deleted session when its successful save returns late', async () => {
    state.view = 'chat'; const gate = deferred<Session>(); let saved: Session | undefined;
    state.apiHook = (path, options) => {
      if (path === '/sessions/a' && options.method === 'PUT') {
        const data = JSON.parse(options.body as string);
        if (data.messages.some((m: { role: string; status: string }) => m.role === 'assistant' && m.status === 'completed')) {
          saved = { ...state.sessions.a, ...data, revision: data.revision + 1 }; state.sessions.a = saved!; return gate.promise;
        }
      }
    };
    render(<Chat/>); await screen.findByText('Session a', { selector: 'strong' });
    fireEvent.change(screen.getByLabelText('输入文献问题'), { target: { value: 'deleted' } }); fireEvent.click(screen.getByLabelText('发送问题')); await screen.findByText('Answer deleted');
    fireEvent.click(screen.getByLabelText('删除会话 Session a')); fireEvent.click(screen.getByText('确认删除')); await waitFor(() => expect(state.sessions.a).toBeUndefined());
    await act(async () => gate.resolve(saved!)); expect(screen.queryByText('Answer deleted')).toBeNull(); expect(screen.queryByRole('button', { name: /^Session a/ })).toBeNull(); expect(screen.queryByText('重试保存回答')).toBeNull();
  });
});
