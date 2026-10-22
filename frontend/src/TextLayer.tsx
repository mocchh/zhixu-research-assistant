import { useEffect, useRef, useState } from 'react';
import { api } from './api';
interface Layer { width: number; height: number; mode: 'native' | 'ocr'; spans: { text: string; bbox: number[]; font_size: number }[] }
export default function TextLayer({ paperId, page, sourceRevision }: { paperId: string; page: number; sourceRevision: string }) {
  const [layer, setLayer] = useState<Layer | null>(null);
  const [width, setWidth] = useState(0);
  const [error, setError] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController(); setLayer(null); setError('');
    api<Layer>(`/papers/${paperId}/pages/${page}/text-layer`, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setLayer(result); }).catch(() => { if (!controller.signal.aborted) setError('本页文字层暂不可用'); });
    return () => controller.abort();
  }, [paperId, page, sourceRevision]);
  useEffect(() => { if (!ref.current) return; const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width)); observer.observe(ref.current); return () => observer.disconnect(); }, []);
  return <div ref={ref} className="pdf-text-layer" aria-label={error || `${layer?.mode === 'ocr' ? 'OCR' : '原生'}可选文字层`}>{layer?.spans.map((span, index) => <span key={index} style={{ left: `${span.bbox[0] / layer.width * 100}%`, top: `${span.bbox[1] / layer.height * 100}%`, width: `${(span.bbox[2] - span.bbox[0]) / layer.width * 100}%`, height: `${(span.bbox[3] - span.bbox[1]) / layer.height * 100}%`, fontSize: Math.max(1, span.font_size * width / layer.width) }}>{span.text}{'\n'}</span>)}{error && <small className="text-layer-error">{error}</small>}</div>;
}
