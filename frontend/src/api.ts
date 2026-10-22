export interface Paper {
  parser_version?: string; id: string; title: string; filename: string; authors: string[]; abstract: string;
  arxiv_id: string | null; version: number | null; family_id: string; page_count: number;
  processed_pages: number; status: 'queued' | 'processing' | 'ready' | 'partial' | 'error' | 'paused' | 'cancelled';
  error: string | null; created_at: string; tags: string[]; is_favorite: boolean;
  duplicate_of: string | null; warnings: string[]; source: 'upload' | 'sample' | 'arxiv'; text_chars: number; ocr_pages: number;
}
export interface Block { id: string; paper_id: string; page: number; text: string; kind: string; bbox: number[]; section: string; order_index: number }
export interface Evidence { id: string; paper_id: string; paper_title: string; page: number; text: string; bbox: number[]; score: number }
export interface Job { id: string; kind: string; status: string; progress: number; message: string; paper_id: string | null; created_at: string; updated_at: string }
export interface JobListing { jobs: Job[]; active_jobs: Job[]; history_jobs: Job[]; next_cursor: string | null; history_total: number }
export interface Health { parser_version?: string; status: string; model_configured: boolean; model: string; parser: string; ocr_available: boolean }
export interface Stats { papers: number; ready: number; pages: number; processing: number; families: number }
export interface AnswerAudit { status: 'checked' | 'fallback' | 'insufficient'; raw_response: string; draft: string; semantic_entailment_verified: false; issues: string[]; citation_checks: Record<string, unknown> }
export interface Answer { answer: string; sources: Evidence[]; mode: 'model' | 'extractive'; warnings: string[]; answer_audit?: AnswerAudit }
export interface PageData { page: number; width: number; height: number; blocks: Block[]; translation: string | null }
export interface PaperDetail { paper: Paper; sections: { title: string; page: number; block_id: string; level: number }[]; versions: Paper[] }
export interface ImportResult { papers: Paper[]; duplicates: { filename?: string; paper_id?: string; title?: string }[]; errors?: { filename: string; detail: string; status: number }[]; status?: 'ok' | 'partial' | 'failed' }
export interface ModelSettings { model: string; base_url: string; has_api_key: boolean; timeout_seconds: number }
export interface CitationEvidence { source_paper_id: string; source_version: number | null; page: number; block_id: string; bbox: number[]; text: string; linked_paper_id: string; linked_version: number | null; match_rule: string; match_note: string }
export interface GraphData { nodes: { id: string; title: string; year?: number; citations: number }[]; edges: { source: string; target: string; confidence: number; evidence?: CitationEvidence[] }[]; scope: string }
export interface DiscoveredPaper { arxiv_id: string; title: string; authors: string[]; abstract: string; published: string; url: string; pdf_url: string }

export interface ApiOptions extends RequestInit { timeoutMs?: number }
export class HttpError extends Error { constructor(message: string, public status: number, public payload?: unknown) { super(message); this.name = 'HttpError'; } }
export function failedImport(error: unknown): ImportResult | null {
  if (!(error instanceof HttpError) || !error.payload || typeof error.payload !== 'object') return null;
  const value = error.payload as Partial<ImportResult>;
  return Array.isArray(value.papers) && Array.isArray(value.duplicates) && Array.isArray(value.errors) ? value as ImportResult : null;
}

const activeRequests = new Set<AbortController>();
let pageLeaving = false;
if (typeof window !== 'undefined') {
  // beforeunload can be cancelled by the user. Only stop work once the page
  // actually leaves, including follow-up fetches queued by a save's catch/finally.
  window.addEventListener('pagehide', event => {
    // A cached document pauses and later resumes its existing requests.
    if (event.persisted) return;
    pageLeaving = true;
    for (const controller of activeRequests) controller.abort();
  });
  // The same document can resume from the browser's back/forward cache.
  window.addEventListener('pageshow', () => { pageLeaving = false; });
}

export async function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  if (pageLeaving) throw new DOMException('The page is leaving.', 'AbortError');
  const { timeoutMs = 120_000, ...init } = options;
  const controller = new AbortController();
  activeRequests.add(controller);
  let timedOut = false;
  const cancel = () => controller.abort();
  if (init.signal?.aborted) controller.abort();
  else init.signal?.addEventListener('abort', cancel, { once: true });
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, signal: controller.signal, headers: { ...(init.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...init.headers } });
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok) {
      const error = contentType.includes('application/json') ? await response.json() : null;
      const detail = typeof error?.detail === 'string' ? error.detail : Array.isArray(error?.detail) ? error.detail.map((item: {msg?: string}) => item.msg).filter(Boolean).join('；') : null;
      throw new HttpError(detail || `请求失败（${response.status}），请稍后重试。`, response.status, error);
    }
    if (!contentType.includes('application/json')) throw new Error('服务没有返回预期数据，请检查 API 服务地址。');
    return await response.json() as T;
  } catch (error) {
    if (timedOut) throw new Error('请求超时。已停止等待，可重试；已提交的后台任务仍可在任务列表查看。');
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    if (error instanceof TypeError) throw new Error('无法连接服务。请检查后端是否已启动。');
    throw error;
  } finally {
    activeRequests.delete(controller);
    clearTimeout(timer);
    init.signal?.removeEventListener('abort', cancel);
  }
}
export function post<T>(path: string, body: unknown = {}, options: ApiOptions = {}) { return api<T>(path, { ...options, method: 'POST', body: JSON.stringify(body) }); }
export function isAbort(error: unknown) { return error instanceof DOMException && error.name === 'AbortError'; }
export function errorText(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请重试。'; }
export function running(job: Job) { return ['queued', 'pending', 'processing', 'running', 'pause_requested', 'cancel_requested'].includes(job.status); }
export function actionable(job: Job) { return running(job) || ['paused', 'error'].includes(job.status); }
export function titleOf(paper: Paper) { return paper.title || paper.filename; }
export function safeUrl(value: string) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? value : undefined; } catch { return undefined; } }
