import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowLeft, BookOpen, ChevronLeft, ChevronRight, Columns2, FileText, Highlighter, Languages, List, Maximize2, Minus, Plus, RefreshCw, X } from 'lucide-react';
import { api, errorText, post, titleOf, type Block, type PageData, type PaperDetail } from './api';
import { useWorkspace } from './store';
import { Empty, ErrorBox, Markdown, Notice, Spinner, Status } from './ui';
import { DocumentTools, TranslationPlanner } from './ReaderExtras';
import { useReadingProgress } from './reading';
import TextLayer from './TextLayer';
import AlignedTranslation from './AlignedTranslation';
import SelectionTranslation from './SelectionTranslation';

function pageSourceRevision(value: PageData | null): string {
  // Match the fields covered by the backend's aligned-translation signature.
  return value ? JSON.stringify(value.blocks.map(({ id, text, kind, bbox, order_index }) => ({ id, text, kind, bbox, order_index }))) : 'pending';
}

export default function Reader() {
  const { view, paperId, page, setPage, highlight, setHighlight, openPaper, papers, revision, health, setView, notify } = useWorkspace();
  const [detail, setDetail] = useState<PaperDetail | null>(null);
  const [pageData, setPageData] = useState<(PageData & { paperId: string; ocr?: boolean }) | null>(null);
  const [loading, setLoading] = useState(false);
  const [pageLoading, setPageLoading] = useState(false);
  const [error, setError] = useState('');
  const [pageError, setPageError] = useState('');
  const [zoom, setZoom] = useState(100);
  const [compare, setCompare] = useState(false);
  const [showText, setShowText] = useState(false);
  const [translation, setTranslation] = useState('');
  const [translating, setTranslating] = useState(false);
  const [language, setLanguage] = useState<'zh' | 'en'>('zh');
  const [selectableText, setSelectableText] = useState(false);
  const [aligned, setAligned] = useState(false);
  const [blockTranslations, setBlockTranslations] = useState<Record<string, string>>({});
  const [translatingBlock, setTranslatingBlock] = useState<string | null>(null);
  const [imageError, setImageError] = useState(false);
  const [imageLoaded, setImageLoaded] = useState(0);
  const [readerNonce, setReaderNonce] = useState(0);
  const paperScroll = useRef<HTMLDivElement>(null);
  const readerRoot = useRef<HTMLElement>(null);
  const pageRequest = useRef<AbortController | null>(null);
  const blockRequest = useRef<AbortController | null>(null);
  const readyPage = pageData?.page === page && pageData.paperId === paperId ? pageData : null;
  const sourceRevision = pageSourceRevision(readyPage);
  const contextKey = JSON.stringify([paperId, page, language, sourceRevision]);
  const requestContext = useRef('');
  requestContext.current = contextKey;
  const saveReadingPosition = useReadingProgress(paperId, page, view === 'reader', imageLoaded, highlight, paperScroll, notify);
  const paper = papers.find(item => item.id === paperId) || detail?.paper;
  useEffect(() => {
    if (!paperId) return;
    const controller = new AbortController(); setError(''); setLoading(true);
    api<PaperDetail>(`/papers/${paperId}`, { signal: controller.signal }).then(setDetail).catch(error => { if (!controller.signal.aborted) setError(errorText(error)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [paperId, revision, readerNonce]);
  useEffect(() => {
    if (!paperId) return;
    const controller = new AbortController(); setPageLoading(true); setPageError(''); setImageError(false);
    api<PageData>(`/papers/${paperId}/pages/${page}?target_language=${language}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      const sameSource = JSON.stringify([paperId, page, language, pageSourceRevision(result)]) === requestContext.current;
      setPageData({ ...result, paperId });
      setTranslation(current => result.translation || (sameSource ? current : ''));
    }).catch(error => { if (!controller.signal.aborted) { setPageError(errorText(error)); setPageData(null); setTranslation(''); } }).finally(() => { if (!controller.signal.aborted) setPageLoading(false); });
    return () => controller.abort();
  }, [paperId, page, revision, readerNonce, language]);
  useEffect(() => { setPageData(null); setTranslation(''); setBlockTranslations({}); paperScroll.current?.scrollTo({ top: 0 }); }, [paperId, page, language]);
  useEffect(() => {
    setBlockTranslations({}); setTranslating(false); setTranslatingBlock(null);
    return () => { pageRequest.current?.abort(); blockRequest.current?.abort(); };
  }, [contextKey]);
  useEffect(() => {
    if (view !== 'reader' || !highlight || highlight.bbox?.length !== 4 || highlight.paper_id !== paperId || highlight.page !== page || !pageData || pageData.paperId !== paperId || pageData.page !== page) return;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const revealEvidence = () => {
      const scroller = paperScroll.current;
      const image = scroller?.querySelector<HTMLImageElement>('.paper-image');
      const mark = scroller?.querySelector<HTMLElement>('.evidence-highlight');
      if (!scroller || !image?.complete || !image.naturalWidth || !mark || !scroller.clientHeight || !image.clientHeight) {
        if (attempts++ < 8) retryTimer = setTimeout(revealEvidence, 100);
        return;
      }
      const paneRect = scroller.getBoundingClientRect();
      const markRect = mark.getBoundingClientRect();
      // Use the rendered highlight rather than PDF coordinates: this also accounts
      // for zoom, pane padding, error banners, and a reader becoming visible again.
      const top = scroller.scrollTop + markRect.top - paneRect.top - 60;
      const left = scroller.scrollLeft + markRect.left - paneRect.left - 30;
      scroller.scrollTo({ top: Math.max(0, top), left: Math.max(0, left), behavior: 'instant' });
    };
    revealEvidence();
    return () => clearTimeout(retryTimer);
  }, [view, highlight, paperId, page, pageData, imageLoaded, zoom, compare, loading]);
  const translatePage = async () => {
    if (!health?.model_configured) { setView('settings'); return; }
    const context = requestContext.current;
    pageRequest.current?.abort(); const controller = new AbortController(); pageRequest.current = controller;
    setTranslating(true); setCompare(true);
    try { const result = await post<{ translation: string }>('/translate', { paper_id: paperId, page, target_language: language }, { signal: controller.signal }); if (!controller.signal.aborted && context === requestContext.current) setTranslation(result.translation); }
    catch (error) { if (!controller.signal.aborted && context === requestContext.current) notify(errorText(error), 'error'); }
    finally { if (pageRequest.current === controller) { pageRequest.current = null; setTranslating(false); } }
  };
  const translateBlock = async (block: Block) => {
    if (!health?.model_configured) { setView('settings'); return; }
    const context = requestContext.current;
    blockRequest.current?.abort(); const controller = new AbortController(); blockRequest.current = controller;
    setTranslatingBlock(block.id);
    try { const result = await post<{ translation: string }>('/translate', { paper_id: paperId, text: block.text, target_language: language }, { signal: controller.signal }); if (!controller.signal.aborted && context === requestContext.current) setBlockTranslations(values => ({ ...values, [block.id]: result.translation })); }
    catch (error) { if (!controller.signal.aborted && context === requestContext.current) notify(errorText(error), 'error'); }
    finally { if (blockRequest.current === controller) { blockRequest.current = null; setTranslatingBlock(null); } }
  };
  const focusBlock = (block: Block) => {
    if (!paperId || !paper) return;
    setHighlight({ id: block.id, paper_id: paperId, paper_title: titleOf(paper), page, text: block.text, bbox: block.bbox, score: 1 });
  };
  if (!paperId) return <div className="page"><div className="page-heading"><div><div className="eyebrow">READ WITH CONTEXT</div><h1>阅读工作台<span className="title-period">.</span></h1><p>原始排版与结构化阅读，在同一处相遇。</p></div></div><Empty title="选择一篇文献开始阅读" text="打开文献库中的论文，即可查看原文、章节导航和中英对照。" action={<button className="button primary" onClick={() => setView('library')}>前往文献库 <ChevronRight size={16} /></button>} /><div className="reader-suggestions">{papers.slice(0, 4).map(p => <button key={p.id} onClick={() => openPaper(p.id)}><FileText size={18} /><span>{titleOf(p)}</span><ChevronRight size={16} /></button>)}</div></div>;
  if (error) return <div className="page"><ErrorBox>{error}</ErrorBox><button className="button" onClick={() => setView('library')}><ArrowLeft size={15} />返回文献库</button></div>;
  if (!paper || (loading && detail?.paper.id !== paperId)) return <div className="loading-area"><Spinner label="正在打开文献…" /></div>;
  const activeHighlight = highlight?.paper_id === paperId && highlight.page === page ? highlight : null;
  const pageCount = paper.page_count || 1;
  return <div className="reader-page"><div className="reader-heading"><button className="icon-button" aria-label="返回文献库" onClick={() => setView('library')}><ArrowLeft size={18} /></button><div><span className="eyebrow">READING ROOM</span><h1>{titleOf(paper)}</h1><p>{paper.authors.slice(0, 4).join(' · ') || paper.filename}</p></div><Status paper={paper} /><a className="button small" href={`/api/papers/${paperId}/file`} target="_blank" rel="noreferrer"><ArrowDownToLine size={15} />PDF</a></div>
    <div className="reader-body"><aside className="reader-outline"><div className="outline-title"><List size={16} /><strong>文献目录</strong></div><div className="outline-info"><span>{paper.page_count} 页</span>{paper.version && <span>版本 v{paper.version}</span>}</div>{detail?.versions.length ? <div className="version-switch"><label htmlFor="version-select">论文版本</label><select id="version-select" value={paperId} onChange={event => openPaper(event.target.value)}>{[paper, ...detail.versions.filter(p => p.id !== paperId)].map(p => <option key={p.id} value={p.id}>{p.version ? `v${p.version} · ${p.ocr_pages > 0 ? '扫描/OCR · ' : ''}${p.filename}` : p.filename}{p.id === paperId ? ' · 当前' : ''}</option>)}</select><small>不同版本分别保留原文与引用定位。</small></div> : null}<nav className="outline-list">{detail?.sections.map((section, index) => <button key={`${section.block_id}-${index}`} className={section.page === page ? 'active' : ''} style={{ paddingLeft: `${12 + Math.max(0, section.level - 1) * 10}px` }} onClick={() => setPage(section.page)}><span>{section.title}</span><small>{section.page}</small></button>)}{!detail?.sections.length && <p className="muted-text">暂未识别到章节目录。你仍然可以按页阅读。</p>}</nav><div className="outline-bottom"><BookOpen size={18} /><span>忠于原文，留有依据。<small>{paper.ocr_pages ? `${paper.ocr_pages} 页通过 OCR 识别` : '保留 PDF 原始页面排版'}</small></span></div></aside>
      <section className="reader-main" ref={readerRoot}><div className="reader-toolbar"><div className="page-controls"><button className="icon-button" disabled={page <= 1} aria-label="上一页" onClick={() => setPage(page - 1)}><ChevronLeft size={17} /></button><label><input type="number" min={1} max={pageCount} key={`${paperId}-${page}`} defaultValue={page} aria-label="当前页码" onBlur={event => { const value = Number(event.currentTarget.value); if (Number.isInteger(value) && value >= 1 && value <= pageCount) setPage(value); else event.currentTarget.value = String(page); }} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /><span>/ {paperCountLabel(pageCount)}</span></label><button className="icon-button" disabled={page >= pageCount} aria-label="下一页" onClick={() => setPage(page + 1)}><ChevronRight size={17} /></button></div><div className="zoom-controls"><button className="icon-button" disabled={zoom <= 50} aria-label="缩小" onClick={() => setZoom(value => Math.max(50, value - 10))}><Minus size={15} /></button><span>{zoom}%</span><button className="icon-button" disabled={zoom >= 200} aria-label="放大" onClick={() => setZoom(value => Math.min(200, value + 10))}><Plus size={15} /></button><button className="icon-button" aria-label="适应页面" onClick={() => setZoom(100)}><Maximize2 size={15} /></button></div><div className="reader-tools"><button className={`button small ${selectableText ? 'selected-button' : ''}`} onClick={() => setSelectableText(!selectableText)}>可选文字层</button><button className={`button small ${showText ? 'selected-button' : ''}`} onClick={() => setShowText(!showText)}><FileText size={14} />段落</button><button className={`button small ${aligned ? 'selected-button' : ''}`} onClick={() => setAligned(!aligned)}>句段对照</button><button className={`button small ${compare ? 'selected-button' : ''}`} onClick={() => setCompare(!compare)}><Columns2 size={14} />中英对照</button><button className="button small primary" disabled={translating || !readyPage?.blocks.length} onClick={() => void translatePage()}>{translating ? <Spinner label="翻译中" /> : <><Languages size={14} />翻译本页</>}</button></div></div>
      <DocumentTools paper={paper} page={page} detail={detail}/><TranslationPlanner paper={paper} language={language} setLanguage={setLanguage}/>
      <SelectionTranslation key={`selection:${paperId}:${page}:${language}:${sourceRevision}`} paperId={paperId} page={page} language={language} root={readerRoot} configured={!!health?.model_configured} configure={() => setView('settings')}/>{aligned && <AlignedTranslation key={`alignment:${paperId}:${page}:${language}:${sourceRevision}`} paperId={paperId} page={page} language={language} configured={!!health?.model_configured} configure={() => setView('settings')} focus={focusBlock} highlightedBlock={highlight?.paper_id === paperId && highlight.page === page ? highlight.id : undefined}/>}
      {activeHighlight && <div className="highlight-banner"><Highlighter size={15} /><span>已定位引用证据 · 第 {page} 页</span><button className="icon-button" aria-label="关闭高亮" onClick={() => setHighlight(null)}><X size={14} /></button></div>}
      {paper.status !== 'ready' && <div className="reader-notice"><Notice>{paper.status === 'processing' || paper.status === 'queued' ? `文献仍在解析，已处理 ${paper.processed_pages} / ${paper.page_count} 页。原始页面可先阅读。` : paper.status === 'paused' ? `解析已暂停，已处理 ${paper.processed_pages} / ${paper.page_count} 页。可在文献库的后台任务中恢复，原始页面仍可阅读。` : paper.status === 'cancelled' ? `解析已取消，已完成的内容仍保留。可在文献库的更多操作中重新解析，原始页面仍可阅读。` : paper.error || '部分内容尚未成功解析，请结合原文核对。'}</Notice></div>}
      <div className={`reader-columns ${compare ? 'comparison' : ''}`}><div className="original-pane"><div className="pane-label"><span><span className="tiny-dot" />原文 PDF</span><small>第 {page} 页</small></div><div className="paper-scroll" ref={paperScroll} onScroll={saveReadingPosition}>{pageError && <div className="inset"><ErrorBox>{pageError}</ErrorBox><button className="button small" onClick={() => setReaderNonce(value => value + 1)}><RefreshCw size={14} />重新加载</button></div>}<div className="paper-canvas" style={{ width: `${zoom}%`, minWidth: `${zoom}%` }}><img key={`${paperId}-${page}-${readerNonce}`} className="paper-image" src={`/api/papers/${paperId}/pages/${page}/image?scale=1.5`} alt={`${titleOf(paper)}，第 ${page} 页原文`} onError={() => setImageError(true)} onLoad={() => setImageLoaded(value => value + 1)} />{selectableText && <TextLayer paperId={paperId} page={page} sourceRevision={`${sourceRevision}:${readyPage?.ocr ?? "pending"}`}/>}{activeHighlight && readyPage && activeHighlight.bbox?.length === 4 && <div className="evidence-highlight" style={{ left: `${activeHighlight.bbox[0] / readyPage.width * 100}%`, top: `${activeHighlight.bbox[1] / readyPage.height * 100}%`, width: `${(activeHighlight.bbox[2] - activeHighlight.bbox[0]) / readyPage.width * 100}%`, height: `${(activeHighlight.bbox[3] - activeHighlight.bbox[1]) / readyPage.height * 100}%` }} title={activeHighlight.text} />}</div>{imageError && <div className="inset"><ErrorBox>本页图片加载失败。可重试，或打开原始 PDF。</ErrorBox><button className="button small" onClick={() => setReaderNonce(value => value + 1)}>重试页面</button></div>}{pageLoading && !pageData && <div className="reader-page-loading"><Spinner label="正在加载页面…" /></div>}</div></div>
      {compare && <div className="translation-pane"><div className="pane-label"><span><Languages size={14} />{language === 'zh' ? '中文' : '英文'}译文</span></div><div className="translation-scroll">{translation ? <><div className="translation-heading">第 {page} 页 <span>与原文按页对应</span></div><Markdown text={translation} /></> : <Empty icon={<Languages size={30} />} title={translating ? '正在翻译这一页' : '让理解，少一道语言障碍'} text={!health?.model_configured ? '翻译需要配置语言模型。原文与译文按当前页对应显示。' : '翻译本页，或提交整篇翻译。公式与代码应结合原文核对。'} action={translating ? <Spinner label="正在生成译文…" /> : <button className="button primary" disabled={!readyPage?.blocks.length} onClick={() => void translatePage()}>{health?.model_configured ? '翻译本页' : '配置模型'}</button>} />}</div></div>}</div>
      {showText && <section className="blocks-panel"><div className="blocks-heading"><div><h3>本页解析段落</h3><p>选择原文可划词翻译；也可定位到 PDF 或逐段翻译。</p></div><span>{readyPage?.blocks.length || 0} 个文本块</span></div>{pageLoading && !readyPage ? <Spinner /> : readyPage?.blocks.map(block => <article className={`text-block ${highlight?.id === block.id ? 'active' : ''}`} key={block.id}><div className="block-tools"><span>{block.kind === 'heading' ? '标题' : block.kind === 'table' ? '表格' : block.kind === 'formula' ? '公式' : block.kind === 'reference' ? '参考文献' : '段落'}</span><div><button className="text-button" onClick={() => focusBlock(block)}><Highlighter size={12} />定位</button><button className="text-button" disabled={translatingBlock !== null} onClick={() => void translateBlock(block)}>{translatingBlock === block.id ? '正在翻译…' : '翻译'}</button></div></div><p className="text-block-source">{block.text}</p>{blockTranslations[block.id] && <div className="block-translation"><Markdown text={blockTranslations[block.id]} /></div>}</article>)}{!readyPage?.blocks.length && <p className="muted-text">本页暂无解析文本。请等待解析完成，或查看原始页面。</p>}</section>}
      </section></div>
  </div>;
}
function paperCountLabel(value: number) { return `${value} 页`; }
