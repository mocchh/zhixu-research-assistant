import { afterEach, describe, expect, it, vi } from 'vitest';
import { downloadApiFile, mergeMessages, saveMessages, type SavedMessage } from './persistence';
import { RequestScope } from './requestScope';
const message = (id: string, content: string, status: SavedMessage['status'] = 'completed'): SavedMessage => ({ id, content, status, role:'assistant',paper_ids:['paper-a'],sources:[],warnings:[] });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe('durable message updates', () => {
  it('preserves another tab’s messages while updating one answer', () => {
    const original = [message('a','pending','pending'),message('b','other tab')];
    const merged = mergeMessages(original,[message('a','answer')]);
    expect(merged.map(item => item.content)).toEqual(['answer','other tab']);
    expect(original[0].content).toBe('pending');
  });
  it('does not resurrect a stopped request with a delayed pending write', () => {
    expect(mergeMessages([message('a','stopped','error')],[message('a','','pending')])[0].status).toBe('error');
  });
  it('rebases a conflicting PUT rather than overwriting concurrent messages', async () => {
    const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status,headers:{'Content-Type':'application/json'}});
    const base = {id:'s',title:'Research',paper_ids:['p'],messages:[message('a','draft','pending')],revision:1};
    const concurrent = {...base,revision:2,messages:[...base.messages,message('b','saved elsewhere')]};
    const fetchMock = vi.fn().mockResolvedValueOnce(response(base)).mockResolvedValueOnce(response({detail:'conflict'},409)).mockResolvedValueOnce(response(concurrent)).mockImplementationOnce(async (_url, options) => response({...JSON.parse(options.body),id:'s',revision:3}));
    vi.stubGlobal('fetch',fetchMock);
    const saved = await saveMessages('s',[message('a','finished')]);
    expect(saved.messages.map(item => item.content)).toEqual(['finished','saved elsewhere']);
    expect(JSON.parse(fetchMock.mock.calls[3][1].body).revision).toBe(2);
  });
});
describe('request ownership', () => {
  it('rejects results from a previous conversation even if transport ignores abort', () => {
    const gate = new RequestScope(); const first = gate.begin('session-a'); const second = gate.begin('session-b');
    expect(first.signal.aborted).toBe(true); expect(first.isCurrent()).toBe(false); expect(second.isCurrent()).toBe(true);
    gate.cancel(); expect(second.isCurrent()).toBe(false);
  });
});
it('shows a translation export conflict instead of downloading an error document', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'暂无对应语言译文'}), {status:409,headers:{'Content-Type':'application/json'}})));
  const create = vi.fn(); vi.stubGlobal('URL', {createObjectURL:create});
  await expect(downloadApiFile('/papers/p/translations/export?format=md','translation.md')).rejects.toThrow('暂无对应语言译文');
  expect(create).not.toHaveBeenCalled();
});
