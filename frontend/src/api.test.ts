import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, HttpError, post } from './api';
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('API request lifecycle', () => {
  it('aborts stalled requests and explains the timeout', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))));
    vi.stubGlobal('fetch', fetchMock);
    const request = api('/chat', { timeoutMs: 50 });
    const assertion = expect(request).rejects.toThrow('请求超时');
    await vi.advanceTimersByTimeAsync(51); await assertion;
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('keeps a user stop distinguishable from a server error', async () => {
    vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))));
    const controller = new AbortController(); const request = post('/chat', {}, { signal: controller.signal });
    controller.abort(); await expect(request).rejects.toMatchObject({ name: 'AbortError' });
  });
  it('preserves HTTP status for optimistic concurrency conflicts', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'revision conflict'}), {status:409,headers:{'Content-Type':'application/json'}})));
    try { await api('/sessions/a'); throw new Error('expected failure'); } catch (error) { expect(error).toBeInstanceOf(HttpError); expect(error).toMatchObject({status:409,message:'revision conflict'}); }
  });
});
