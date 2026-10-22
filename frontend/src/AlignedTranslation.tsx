import { useEffect, useRef, useState } from 'react';
import { api, errorText, isAbort, post, type Block } from './api';
import { ErrorBox, Spinner } from './ui';
import { downloadApiFile } from './persistence';

interface Segment { id: string; block_id: string; page: number; start: number; end: number; source: string; bbox: number[]; kind: string; translation: string | null }
interface Alignment { paper_id: string; page: number; target_language: string; source_hash: string; segments: Segment[]; cached: boolean; notes: string[] }
interface Props { paperId: string; page: number; language: 'zh' | 'en'; configured: boolean; configure: () => void; focus: (block: Block) => void; highlightedBlock?: string }

export default function AlignedTranslation({ paperId, page, language, configured, configure, focus, highlightedBlock }: Props) {
  const [value, setValue] = useState<Alignment | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const request = useRef<AbortController | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const path = `/papers/${paperId}/pages/${page}/aligned-translation`;
  useEffect(() => {
    const controller = new AbortController(); request.current?.abort(); request.current = controller;
    setValue(null); setError(''); setBusy(false); setLoading(true);
    api<Alignment>(`${path}?target_language=${language}`, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setValue(result); })
      .catch(error => { if (!controller.signal.aborted) setError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, language]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    const selected = container.current?.querySelector<HTMLElement>('.aligned-pair.active');
    selected?.scrollIntoView?.({ block: 'nearest', behavior: 'instant' });
  }, [highlightedBlock, value]);
  async function translate() {
    if (!configured) { configure(); return; }
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError('');
    try {
      const result = await post<Alignment>(path, { target_language: language }, { signal: controller.signal, timeoutMs: 600_000 });
      if (!controller.signal.aborted) setValue(result);
    } catch (error) { if (!isAbort(error) && !controller.signal.aborted) setError(errorText(error)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  function select(segment: Segment) {
    focus({ id: segment.block_id, paper_id: paperId, page, bbox: segment.bbox, text: segment.source, kind: segment.kind, section: '', order_index: 0 });
  }
  const complete = !!value?.segments.length && value.segments.every(s => s.translation !== null);
  return <section className="aligned-translation" aria-label="句段对照翻译">
    <div className="aligned-actions"><div><h3>句段对照</h3><p>点原文或译文定位 PDF 文本块；已生成的配对会保存。</p></div>
      {!complete && <button className="button small primary" disabled={busy || loading || !value?.segments.length} onClick={() => void translate()}>{busy ? <Spinner label="正在逐段翻译…" /> : configured ? '生成本页句段译文' : '配置模型后翻译'}</button>}
      {complete && <><button className="button small" onClick={() => void downloadApiFile(`${path}/export?target_language=${language}&format=md`, `对照-p${page}.md`).catch(error => setError(errorText(error)))}>对照 MD</button><button className="button small" onClick={() => void downloadApiFile(`${path}/export?target_language=${language}&format=html`, `对照-p${page}.html`).catch(error => setError(errorText(error)))}>对照 HTML</button></>}
      {busy && <button className="button small" onClick={() => { request.current?.abort(); setBusy(false); setError('已停止等待，本次未完成译文不会作为完整配对保存。'); }}>停止</button>}
    </div>
    {error && <ErrorBox>{error}</ErrorBox>}{loading && <Spinner label="正在读取原文配对…" />}
    <div className="aligned-pairs" ref={container}>{value?.segments.map((segment, i) => <article key={segment.id} className={`aligned-pair ${highlightedBlock === segment.block_id ? 'active' : ''}`}>
      <button className="aligned-source" aria-label={`定位原文片段 ${i + 1}`} onClick={() => select(segment)}><small>原文 {i + 1}</small><span>{segment.source}</span></button>
      <button className="aligned-target" aria-label={`定位译文片段 ${i + 1}`} onClick={() => select(segment)}><small>{language === 'zh' ? '中文' : '英文'}</small><span>{segment.translation || '尚未生成译文'}</span></button>
    </article>)}</div>
    <p className="aligned-note">按原文编号配对，公式与表格保持整块，长句可能拆段。编号校验不代表翻译语义已核验；定位精度为文本块。</p>
  </section>;
}
