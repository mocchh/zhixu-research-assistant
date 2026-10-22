import { useEffect, useRef, useState, type RefObject } from 'react';
import { errorText, isAbort, post } from './api';
import { ErrorBox, Markdown, Spinner } from './ui';

export function selectedSource(root: HTMLElement | null): string {
  const selection = window.getSelection();
  if (!root || !selection || selection.isCollapsed || !selection.rangeCount) return '';
  const range = selection.getRangeAt(0);
  const sourceElement = (node: Node) => (node instanceof Element ? node : node.parentElement)?.closest('.pdf-text-layer, .text-block-source');
  const start = sourceElement(range.startContainer), end = sourceElement(range.endContainer);
  if (!start || !end || !root.contains(start) || !root.contains(end)) return '';
  // A selection crossing unrelated UI is not a source excerpt.
  if (start !== end) return '';
  return selection.toString().trim();
}

export default function SelectionTranslation({ paperId, page, language, root, configured, configure }: {
  paperId: string; page: number; language: 'zh' | 'en'; root: RefObject<HTMLElement>; configured: boolean; configure: () => void;
}) {
  const [source, setSource] = useState('');
  const [translation, setTranslation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    request.current?.abort(); setSource(''); setTranslation(''); setError(''); setBusy(false);
    const capture = () => {
      const text = selectedSource(root.current);
      if (!text) return;
      request.current?.abort(); setBusy(false); setTranslation('');
      if (text.length > 12000) { setSource(''); setError('选中文字超过 12000 字，请缩小范围或使用整页翻译。'); return; }
      setSource(text); setError('');
    };
    const node = root.current;
    node?.addEventListener('mouseup', capture); node?.addEventListener('keyup', capture); node?.addEventListener('touchend', capture);
    return () => { request.current?.abort(); node?.removeEventListener('mouseup', capture); node?.removeEventListener('keyup', capture); node?.removeEventListener('touchend', capture); };
  }, [paperId, page, language, root]);
  async function translate() {
    if (!configured) { configure(); return; }
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError('');
    try {
      const result = await post<{ translation: string }>('/translate', { paper_id: paperId, page, text: source, target_language: language }, { signal: controller.signal });
      if (!controller.signal.aborted) setTranslation(result.translation);
    } catch (error) { if (!controller.signal.aborted && !isAbort(error)) setError(errorText(error)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!source && !error) return null;
  return <aside className="selection-translation" aria-label="选中文字翻译">
    <div className="selection-translation-actions"><strong>第 {page} 页 · 已选 {source.length} 字</strong>
      <button className="button small primary" disabled={!source || busy} onClick={() => void translate()}>{busy ? <Spinner label="翻译中" /> : configured ? '翻译选中文字' : '配置模型后翻译'}</button>
      <button className="button small" onClick={() => { request.current?.abort(); setBusy(false); setSource(''); setTranslation(''); setError(''); window.getSelection()?.removeAllRanges(); }}>关闭选文</button></div>
    {source && <p className="selected-source">{source}</p>}{error && <ErrorBox>{error}</ErrorBox>}{translation && <div className="selected-translation"><Markdown text={translation} /></div>}
  </aside>;
}
