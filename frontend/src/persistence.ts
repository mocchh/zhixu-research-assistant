import { api, HttpError, type Evidence, type AnswerAudit } from './api';
export interface SavedMessage {
  id: string; role: 'user' | 'assistant'; content: string; paper_ids: string[];
  sources: Evidence[]; mode?: 'model' | 'extractive'; warnings: string[];
  answer_audit?: AnswerAudit;
  status: 'pending' | 'completed' | 'error'; request_id?: string; created_at?: string;
}
export interface Session { id: string; title: string; paper_ids: string[]; messages: SavedMessage[]; revision: number; created_at: string; updated_at: string }
export interface Note { id: string; title: string; content: string; paper_ids: string[]; sources: Evidence[]; kind: 'note' | 'summary' | 'review' | 'ideas' | 'outline'; revision: number; created_at: string; updated_at: string; versions?: {revision: number; title: string; created_at: string}[] }
export interface ReadingPosition { paper_id: string; page: number; scroll_ratio: number; revision: number; updated_at: string | null }

export function mergeMessages(existing: SavedMessage[], updates: SavedMessage[]) {
  const result = existing.map(message => ({ ...message }));
  for (const update of updates) {
    const index = result.findIndex(message => message.id === update.id);
    if (index < 0) result.push(update);
    else if (!(update.status === 'pending' && result[index].status !== 'pending')) result[index] = update;
  }
  return result;
}
/** Always rebase on the latest SQLite revision; never replace unrelated messages. */
export async function saveMessages(id: string, updates: SavedMessage[], paperIds?: string[]) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const current = await api<Session>(`/sessions/${id}`);
    const firstQuestion = updates.find(message => message.role === 'user')?.content.trim();
    const title = ['新的研究对话', '新对话'].includes(current.title) && firstQuestion ? firstQuestion.slice(0, 35) : current.title;
    try {
      return await api<Session>(`/sessions/${id}`, { method: 'PUT', body: JSON.stringify({ revision: current.revision, title, paper_ids: paperIds || current.paper_ids, messages: mergeMessages(current.messages, updates) }) });
    } catch (error) { if (!(error instanceof HttpError) || error.status !== 409 || attempt === 2) throw error; }
  }
  throw new Error('会话在其他窗口发生变化，请重新打开后重试。');
}
export function downloadText(text: string, filename: string, mime = 'text/markdown;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function downloadApiFile(path: string, filename: string) {
  const response = await fetch(`/api${path}`, { signal: AbortSignal.timeout(60_000) });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : `导出失败（${response.status}）`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
