// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, HttpError, post } from './api';
afterEach(() => { window.dispatchEvent(new Event('pageshow')); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('API request lifecycle', () => {
  it('cancels in-flight reads and saves when the page leaves and blocks follow-up requests', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))));
    vi.stubGlobal('fetch', fetchMock);
    const reading = api('/sessions');
    // Saving code can attempt a list refresh even after its request rejects.
    const saving = api('/sessions/a', { method: 'PUT', body: '{}' }).catch(() => api('/sessions'));
    const assertions = [expect(reading).rejects.toMatchObject({ name: 'AbortError' }), expect(saving).rejects.toMatchObject({ name: 'AbortError' })];
    window.dispatchEvent(new Event('pagehide'));
    await Promise.all(assertions);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.every(([, options]) => options.signal.aborted)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('keeps work running if a beforeunload prompt is cancelled', async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))));
    vi.stubGlobal('fetch', fetchMock);
    const request = api('/sessions/a', { signal: controller.signal });
    const assertion = expect(request).rejects.toMatchObject({ name: 'AbortError' });
    const stay = (event: Event) => event.preventDefault();
    window.addEventListener('beforeunload', stay, { once: true });
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(false);
    controller.abort(); await assertion;
  });
  it('preserves pending work when a cached page is suspended and restored', async () => {
    vi.useFakeTimers();
    let finish!: (response: Response) => void;
    const fetchMock = vi.fn((_url, _options) => new Promise<Response>(resolve => { finish = resolve; }));
    vi.stubGlobal('fetch', fetchMock);
    const request = api('/sessions');
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(1);
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    finish(new Response('{"sessions":[]}', { headers: { 'Content-Type': 'application/json' } }));
    await expect(request).resolves.toEqual({ sessions: [] });
    expect(vi.getTimerCount()).toBe(0);
    const next = api('/sessions');
    finish(new Response('{"sessions":[]}', { headers: { 'Content-Type': 'application/json' } }));
    await expect(next).resolves.toEqual({ sessions: [] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(false);
  });
  it('removes completed requests from page-exit cancellation', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"sessions":[]}', { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    await api('/sessions');
    window.dispatchEvent(new Event('pagehide'));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
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
