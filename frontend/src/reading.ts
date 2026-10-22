import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { api, HttpError, type Evidence } from './api';
import type { ReadingPosition } from './persistence';

export function useReadingProgress(paperId: string | null, page: number, visible: boolean, imageLoaded: number, highlight: Evidence | null, pane: RefObject<HTMLDivElement>, report: (message: string) => void) {
  const [stored, setStored] = useState<ReadingPosition | null>(null);
  const positions = useRef(new Map<string, ReadingPosition>());
  const restored = useRef('');
  const saveTimer = useRef<ReturnType<typeof setTimeout>>();
  const queue = useRef(Promise.resolve());
  const current = useRef({ paperId, page }); current.current = { paperId, page };
  useEffect(() => {
    if (!paperId) return; const controller = new AbortController(); setStored(null); restored.current = '';
    api<ReadingPosition>(`/papers/${paperId}/reading`, { signal: controller.signal }).then(value => { positions.current.set(paperId, value); setStored(value); }).catch(() => {});
    return () => controller.abort();
  }, [paperId]);
  const persist = useCallback((id: string, currentPage: number, ratio: number) => {
    queue.current = queue.current.then(async () => {
      const existing = positions.current.get(id) || await api<ReadingPosition>(`/papers/${id}/reading`);
      try { const next = await api<ReadingPosition>(`/papers/${id}/reading`, { method: 'PUT', body: JSON.stringify({ revision: existing.revision, page: currentPage, scroll_ratio: ratio }) }); positions.current.set(id, next); }
      catch (error) { if (error instanceof HttpError && error.status === 409) { positions.current.set(id, await api<ReadingPosition>(`/papers/${id}/reading`)); report('阅读记录已在其他窗口更新，本次旧记录未覆盖。'); } else throw error; }
    }).catch(() => report('阅读位置暂未保存，请检查服务连接。'));
  }, [report]);
  const onScroll = useCallback(() => {
    const { paperId: id, page: currentPage } = current.current; const element = pane.current;
    if (!id || !element || !visible || !stored || stored.paper_id !== id) return;
    clearTimeout(saveTimer.current);
    const ratio = element.scrollTop / Math.max(1, element.scrollHeight - element.clientHeight);
    saveTimer.current = setTimeout(() => persist(id, currentPage, Math.min(1, Math.max(0, ratio))), 500);
  }, [pane, visible, stored, persist]);
  useEffect(() => {
    if (!visible || !paperId || !stored || stored.paper_id !== paperId || !imageLoaded) return;
    const element = pane.current;
    const image = element?.querySelector<HTMLImageElement>('.paper-image');
    if (!element || !image?.complete || !image.naturalWidth) return;
    const key = `${paperId}:${page}`;
    if (restored.current !== key) {
      restored.current = key;
      if (!highlight && stored.page === page) element.scrollTo({ top: stored.scroll_ratio * Math.max(0, element.scrollHeight - element.clientHeight), behavior: 'instant' });
      onScroll();
    }
  }, [paperId, page, visible, imageLoaded, stored, highlight, pane, onScroll]);
  useEffect(() => () => clearTimeout(saveTimer.current), []);
  return onScroll;
}
