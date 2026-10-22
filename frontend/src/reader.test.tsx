// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Block, PageData, Paper } from './api';
import { api, post } from './api';
import Reader from './Reader';
import TextLayer from './TextLayer';

const workspace = vi.hoisted(() => ({ revision: 0, notify: vi.fn() }));
const paper = { id: 'p', title: 'Reader test', filename: 'paper.pdf', authors: [], page_count: 1, status: 'ready', ocr_pages: 0 } as unknown as Paper;
vi.mock('./store', () => ({ useWorkspace: () => ({ ...workspace, view: 'reader', paperId: 'p', page: 1, papers: [paper], health: { model_configured: true }, highlight: null, setPage: vi.fn(), setHighlight: vi.fn(), openPaper: vi.fn(), setView: vi.fn() }) }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: vi.fn(), post: vi.fn() }));
vi.mock('./ReaderExtras', () => ({ DocumentTools: () => null, TranslationPlanner: () => null }));
vi.mock('./reading', () => ({ useReadingProgress: () => vi.fn() }));
vi.mock('./ui', () => ({
  ErrorBox: ({ children }: { children: React.ReactNode }) => <div role="alert">{children}</div>,
  Spinner: ({ label }: { label: string }) => <span>{label}</span>,
  Markdown: ({ text }: { text: string }) => <p>{text}</p>,
  Notice: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Empty: ({ title, action }: { title: string; action: React.ReactNode }) => <div>{title}{action}</div>, Status: () => null,
}));
let pageData: PageData;
const block = (): Block => ({ id: 'same-id', paper_id: 'p', page: 1, text: 'Original sentence. Another sentence.', kind: 'paragraph', bbox: [10, 20, 300, 50], section: '', order_index: 0 });
function sourceCalls() { return vi.mocked(api).mock.calls.filter(([path]) => path.endsWith('/text-layer')); }
function pairCalls() { return vi.mocked(api).mock.calls.filter(([path]) => path.includes('/aligned-translation')); }
beforeEach(() => {
  vi.resetAllMocks(); workspace.revision = 0;
  HTMLElement.prototype.scrollTo = vi.fn(); HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  pageData = { page: 1, width: 595, height: 842, translation: null, blocks: [block()] };
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/papers/p') return { paper, sections: [], versions: [] } as never;
    if (path.endsWith('/text-layer')) return { width: 595, height: 842, mode: 'ocr', spans: pageData.blocks.map(b => ({ text: b.text, bbox: b.bbox, font_size: 12 })) } as never;
    if (path.includes('/aligned-translation')) return { paper_id: 'p', page: 1, target_language: 'zh', source_hash: 'test', cached: false, notes: [], segments: pageData.blocks.map(b => ({ id: b.id, block_id: b.id, page: 1, start: 0, end: b.text.length, source: b.text, bbox: b.bbox, kind: b.kind, translation: null })) } as never;
    return structuredClone(pageData) as never;
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('refreshes an initially empty text layer after OCR completes, and ignores its late old response', async () => {
  let resolveOld!: (value: unknown) => void;
  vi.mocked(api).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }) as never);
  const view = render(<TextLayer paperId="p" page={1} sourceRevision="pending"/>);
  view.rerender(<TextLayer paperId="p" page={1} sourceRevision="parsed"/>);
  await screen.findByText('Original sentence. Another sentence.');
  await act(async () => resolveOld({ width: 595, height: 842, mode: 'native', spans: [] }));
  expect(screen.getByText('Original sentence. Another sentence.')).toBeTruthy();
  expect(vi.mocked(api).mock.calls[0][1]?.signal?.aborted).toBe(true);
  view.rerender(<TextLayer paperId="p" page={1} sourceRevision="parsed"/>);
  expect(api).toHaveBeenCalledTimes(2);
});

it.each(['text', 'kind', 'bbox', 'order_index'] as const)('refreshes both reader source views when only %s changes', async field => {
  const view = render(<Reader/>);
  await waitFor(() => expect((screen.getByRole('button', { name: '翻译本页' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '可选文字层' }));
  fireEvent.click(screen.getByRole('button', { name: '句段对照' }));
  await waitFor(() => { expect(sourceCalls()).toHaveLength(1); expect(pairCalls()).toHaveLength(1); });
  const replacement = { ...block(), [field]: { text: 'Changed source.', kind: 'formula', bbox: [10, 20, 310, 50], order_index: 1 }[field] };
  pageData = { ...pageData, blocks: [replacement] };
  workspace.revision++; view.rerender(<Reader/>);
  await waitFor(() => { expect(sourceCalls()).toHaveLength(2); expect(pairCalls()).toHaveLength(2); });
  workspace.revision++; view.rerender(<Reader/>);
  await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/papers/p')).toHaveLength(3));
  expect(sourceCalls()).toHaveLength(2); expect(pairCalls()).toHaveLength(2);
});

it.each(['page', 'block'] as const)('discards late %s translation after source changes without cancelling unchanged polling', async kind => {
  let resolve!: (value: unknown) => void;
  vi.mocked(post).mockImplementationOnce(() => new Promise(done => { resolve = done; }) as never);
  const view = render(<Reader/>);
  await waitFor(() => expect((screen.getByRole('button', { name: '翻译本页' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '段落' }));
  fireEvent.click(screen.getByRole('button', { name: kind === 'page' ? '翻译本页' : '翻译' }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  const signal = vi.mocked(post).mock.calls[0][2]?.signal;
  workspace.revision++; view.rerender(<Reader/>);
  await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/papers/p')).toHaveLength(2));
  expect(signal?.aborted).toBe(false);
  pageData = { ...pageData, blocks: [{ ...block(), kind: 'formula' }] };
  workspace.revision++; view.rerender(<Reader/>);
  await waitFor(() => expect(signal?.aborted).toBe(true));
  vi.mocked(post).mockResolvedValueOnce({ translation: 'CURRENT RESULT' });
  fireEvent.click(screen.getAllByRole('button', { name: kind === 'page' ? '翻译本页' : '翻译' })[0]);
  await screen.findByText('CURRENT RESULT');
  await act(async () => resolve({ translation: 'STALE RESULT' }));
  expect(screen.queryByText('STALE RESULT')).toBeNull();
  expect(screen.getByText('CURRENT RESULT')).toBeTruthy();
  expect(workspace.notify).not.toHaveBeenCalled();
});
