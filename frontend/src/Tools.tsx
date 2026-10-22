import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { ArrowDownToLine, ArrowRight, ArrowUpRight, BookOpen, Check, CheckCircle2, ExternalLink, GitBranch, KeyRound, Link2, Network, Plus, RefreshCw, Search, Settings2, ShieldCheck } from 'lucide-react';
import { api, errorText, post, safeUrl, type DiscoveredPaper, type GraphData, type ImportResult, type ModelSettings } from './api';
import { useWorkspace } from './store';
import { Empty, ErrorBox, Notice, Spinner } from './ui';

export function Graph() {
  const { view, revision, openPaper } = useWorkspace();
  const [data, setData] = useState<GraphData | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [chosen, setChosen] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (view !== 'graph') return;
    const controller = new AbortController(); setBusy(true); setError('');
    api<GraphData>('/graph', { signal: controller.signal }).then(setData).catch(error => { if (!controller.signal.aborted) setError(errorText(error)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [view, revision, nonce]);
  const positions = useMemo(() => {
    if (!data) return [];
    const nodes = data.nodes.map((node, i) => { const angle = i * Math.PI * (3 - Math.sqrt(5)); const radius = 100 + Math.sqrt(i / Math.max(data.nodes.length, 1)) * 170; return { ...node, x: 390 + Math.cos(angle) * radius, y: 285 + Math.sin(angle) * radius }; });
    for (let iteration = 0; iteration < 90; iteration++) {
      const movement = nodes.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) { const dx = nodes[i].x - nodes[j].x; const dy = nodes[i].y - nodes[j].y; const distance = Math.max(10, Math.sqrt(dx * dx + dy * dy)); const force = 1900 / (distance * distance); movement[i].x += dx / distance * force; movement[i].y += dy / distance * force; movement[j].x -= dx / distance * force; movement[j].y -= dy / distance * force; }
      for (const edge of data.edges) { const i = nodes.findIndex(node => node.id === edge.source); const j = nodes.findIndex(node => node.id === edge.target); if (i < 0 || j < 0 || i === j) continue; const dx = nodes[j].x - nodes[i].x; const dy = nodes[j].y - nodes[i].y; const distance = Math.sqrt(dx * dx + dy * dy); const force = (distance - 155) * .007; movement[i].x += dx / Math.max(1, distance) * force; movement[i].y += dy / Math.max(1, distance) * force; movement[j].x -= dx / Math.max(1, distance) * force; movement[j].y -= dy / Math.max(1, distance) * force; }
      nodes.forEach((node, i) => { node.x = Math.max(65, Math.min(710, node.x + movement[i].x)); node.y = Math.max(60, Math.min(495, node.y + movement[i].y)); });
    }
    return nodes;
  }, [data]);
  const selected = data?.nodes.find(node => node.id === chosen);
  const related = data?.edges.filter(edge => edge.source === chosen || edge.target === chosen) || [];
  return <div className="page"><div className="page-heading"><div><div className="eyebrow">CONNECT THE IDEAS</div><h1>引用图谱<span className="title-period">.</span></h1><p>沿着引用关系，发现文献之间的联系。</p></div><a className="button" href="/api/graph/export?format=mermaid" download>导出 Mermaid</a><a className="button" href="/api/graph/export?format=dot" download>导出 DOT</a><button className="button" disabled={busy} onClick={() => setNonce(value => value + 1)}><RefreshCw size={16} className={busy ? 'spin' : ''} />刷新图谱</button></div><Notice>展示当前文献库内识别到的引用关系；同论文版本已合并。节点数字表示库内被引用次数，点击节点查看详情。</Notice>{error && <ErrorBox>{error}</ErrorBox>}{busy && !data ? <div className="loading-area"><Spinner label="正在连接文献…" /></div> : data?.nodes.length ? <div className="graph-layout"><section className="graph-canvas"><div className="graph-top"><span><Network size={15} />文献关系网络</span><span>{data.nodes.length} 篇文献 · {data.edges.length} 条引用</span></div><svg viewBox="0 0 780 560" role="img" aria-label="文献引用关系图"><defs><pattern id="graph-dots" x="0" y="0" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#cfd8e4" /></pattern><marker id="arrow" viewBox="0 0 10 10" refX="25" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#91a3ba" /></marker></defs><rect width="780" height="560" fill="url(#graph-dots)" />{data.edges.map((edge, index) => { const source = positions.find(node => node.id === edge.source); const target = positions.find(node => node.id === edge.target); if (!source || !target) return null; const active = chosen === source.id || chosen === target.id; return <line key={index} x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke={active ? '#4e76ba' : '#bdcad9'} strokeWidth={active ? 2.2 : 1.3} opacity={chosen && !active ? .3 : .9} markerEnd="url(#arrow)"><title>引用匹配规则评分：{edge.confidence}（非概率）</title></line>; })}{positions.map((node, i) => <g key={node.id} className={`graph-node ${chosen === node.id ? 'active' : ''}`} tabIndex={0} role="button" aria-label={`查看 ${node.title}`} onClick={() => setChosen(node.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setChosen(node.id); } }}><circle cx={node.x} cy={node.y} r={Math.min(28, 15 + node.citations * 2)} fill={chosen === node.id ? '#172d4c' : i % 3 === 0 ? '#457aa8' : '#779cbf'} stroke="white" strokeWidth="4" /><text x={node.x} y={node.y + 4} fill="white" textAnchor="middle" fontSize="10">{node.citations}</text><text x={node.x} y={node.y + Math.min(28, 15 + node.citations * 2) + 18} textAnchor="middle" className="graph-node-label">{node.title.length > 27 ? `${node.title.slice(0, 26)}…` : node.title}</text><title>{node.title}</title></g>)}</svg><div className="graph-legend"><span><span className="legend-dot" />节点大小：库内被引次数</span><span>箭头：引用文献 → 被引文献</span><span>可能存在漏边，请结合原文核对。</span></div>{!data.edges.length && <div className="graph-empty-note">已有文献尚未匹配出内部引用关系。可导入同一方向的更多论文。</div>}</section><aside className="graph-detail">{selected ? <><div className="eyebrow">SELECTED PAPER</div><h2>{selected.title}</h2><div className="graph-number"><strong>{selected.citations}</strong><span>库内被引次数</span></div><button className="button primary" onClick={() => openPaper(selected.id)}><BookOpen size={15} />阅读文献</button><h3>相关连接 <span>{related.length}</span></h3>{related.map((edge, i) => { const isSource = edge.source === chosen; const node = data.nodes.find(node => node.id === (isSource ? edge.target : edge.source)); return node ? <div className="related-connection" key={i}><button className="related-paper" onClick={() => setChosen(node.id)}><small>{isSource ? '引用了' : '被引用于'}</small><strong>{node.title}</strong><span>匹配规则评分 {edge.confidence} · 非概率</span></button>{!!edge.evidence?.length && <details className="citation-evidence"><summary>查看引用依据（{edge.evidence.length}）</summary>{edge.evidence.map((evidence, index) => <div key={`${evidence.source_paper_id}:${evidence.block_id}:${index}`}><p>{evidence.text}</p><small>{evidence.match_note}</small><button className="button small" onClick={() => openPaper(evidence.source_paper_id, evidence.page, { id: evidence.block_id, paper_id: evidence.source_paper_id, paper_title: data.nodes.find(item => item.id === edge.source)?.title || "引用原文", page: evidence.page, bbox: evidence.bbox, text: evidence.text, score: edge.confidence })}>定位引用原文 · 第 {evidence.page} 页{evidence.source_version ? ` · v${evidence.source_version}` : ''}</button></div>)}</details>}</div> : null; })}</> : <><div className="graph-detail-icon"><GitBranch size={26} /></div><h3>每个节点，都是一个研究起点</h3><p>点击图中节点，查看论文与其他文献之间的引用关系。</p><div className="subtle-divider" /><p className="muted-text">引文匹配可能存在遗漏或误差，请结合原始参考文献核对。</p></>}</aside></div> : <Empty icon={<Network size={30} />} title="让你的论文连成网络" text="导入并解析文献后，工作台会识别它们之间的引用关系。" />}</div>;
}

export function Discover() {
  const { importResult } = useWorkspace();
  const [query, setQuery] = useState('');
  const [papers, setPapers] = useState<DiscoveredPaper[]>([]);
  const [busy, setBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState('');
  const [importing, setImporting] = useState<string | null>(null);
  const [imported, setImported] = useState<string[]>([]);
  const [lastQuery, setLastQuery] = useState('');
  const search = async (value = query) => {
    if (!value.trim() || busy) return;
    setBusy(true); setError(''); setQuery(value); setLastQuery(value); setSearched(true);
    try { setPapers((await api<{ papers: DiscoveredPaper[] }>(`/discover?q=${encodeURIComponent(value.trim())}&limit=8`)).papers); }
    catch (error) { setError(errorText(error)); setPapers([]); } finally { setBusy(false); }
  };
  const download = async (paper: DiscoveredPaper) => {
    setImporting(paper.arxiv_id); setError('');
    try { await importResult(await post<ImportResult>('/discover/import', { arxiv_id: paper.arxiv_id })); setImported(previous => [...previous, paper.arxiv_id]); }
    catch (error) { setError(errorText(error)); } finally { setImporting(null); }
  };
  return <div className="page discover-page"><div className="page-heading"><div><div className="eyebrow">STAY CURIOUS</div><h1>论文发现<span className="title-period">.</span></h1><p>发现下一篇值得读的论文，直接加入你的研究。</p></div><span className="service-chip"><span className="tiny-dot green" />来源 arXiv</span></div><section className="discover-hero"><div className="discover-ornament"><Search size={53} /></div><span className="eyebrow">A NEW PERSPECTIVE AWAITS</span><h2>下一条线索，从搜索开始。</h2><form className="discover-search" onSubmit={event => { event.preventDefault(); void search(); }}><Search size={20} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="输入研究主题、论文标题或作者…" aria-label="搜索 arXiv 论文" /><button className="button primary" disabled={busy || !query.trim()}>{busy ? <Spinner label="搜索中" /> : <>搜索文献 <ArrowRight size={15} /></>}</button></form><div className="discover-topics"><span>试试看</span>{['retrieval augmented generation', 'vision language model', 'AI agents'].map(topic => <button key={topic} disabled={busy} onClick={() => void search(topic)}>{topic}<ArrowUpRight size={12} /></button>)}</div></section>{error && <ErrorBox>{error}</ErrorBox>}{busy ? <div className="loading-area"><Spinner label="正在查询 arXiv 元数据…" /></div> : searched ? <><div className="results-heading"><h2>“{lastQuery}” 的搜索结果</h2><span>{papers.length} 篇文献</span></div><div className="discover-results">{papers.map(paper => <article className="discovered-paper" key={paper.arxiv_id}><div className="discovered-meta"><span className="tag">arXiv</span><span>{paper.arxiv_id}</span><span>{paper.published?.slice(0, 10)}</span></div><h3><a href={safeUrl(paper.url)} target="_blank" rel="noreferrer">{paper.title}</a></h3><p className="discovered-authors">{paper.authors.slice(0, 6).join(' · ')}{paper.authors.length > 6 ? ' 等' : ''}</p><p className="discovered-abstract">{paper.abstract}</p><div className="discovered-footer"><a href={safeUrl(paper.url)} target="_blank" rel="noreferrer">查看原文页面 <ExternalLink size={13} /></a><button className={`button small ${imported.includes(paper.arxiv_id) ? '' : 'primary'}`} disabled={importing !== null || imported.includes(paper.arxiv_id)} onClick={() => void download(paper)}>{importing === paper.arxiv_id ? <Spinner label="正在下载" /> : imported.includes(paper.arxiv_id) ? <><Check size={14} />已导入</> : <><Plus size={14} />加入文献库</>}</button></div></article>)}</div>{papers.length === 0 && !error && <Empty icon={<Search size={29} />} title="暂未找到相关文献" text="试着使用英文关键词、缩短论文标题，或调整研究主题。" />}</> : <div className="discover-principles"><div><Search size={22} /><h3>从主题出发</h3><p>通过 arXiv 检索论文与研究线索。</p></div><div><ArrowDownToLine size={22} /><h3>一键导入</h3><p>下载开放论文，自动进入解析流程。</p></div><div><Link2 size={22} /><h3>带回你的研究</h3><p>与已有文献一起阅读、比较和提问。</p></div></div>}</div>;
}

export function Settings() {
  const { view, health, refresh, notify } = useWorkspace();
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState('');
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const requestVersion = useRef(0);
  const mutationInFlight = useRef(false);
  // Hide stale/empty fields before paint on entry. A late settings response
  // must not replace the value while the user is selecting or typing text.
  useLayoutEffect(() => {
    if (view !== 'settings') return;
    if (mutationInFlight.current) { setLoading(false); return; }
    const version = ++requestVersion.current;
    const controller = new AbortController(); setLoading(true); setError('');
    api<ModelSettings>('/settings', { signal: controller.signal }).then(result => { if (controller.signal.aborted || version !== requestVersion.current) return; setSettings(result); setModel(result.model); setBaseUrl(result.base_url); setApiKey(''); }).catch(error => { if (!controller.signal.aborted && version === requestVersion.current) setError(errorText(error)); }).finally(() => { if (!controller.signal.aborted && version === requestVersion.current) setLoading(false); });
    return () => controller.abort();
  }, [view]);
  const sameService = (first: string, second: string) => {
    try { const a = new URL(first.trim()); const b = new URL(second.trim()); return a.origin === b.origin && a.pathname.replace(/\/+$/, '') === b.pathname.replace(/\/+$/, ''); }
    catch { return false; }
  };
  const serviceChanged = settings !== null && !sameService(baseUrl, settings.base_url);
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (mutationInFlight.current) return;
    mutationInFlight.current = true; requestVersion.current++; setLoading(false); setSaving(true); setError(''); setTest(null);
    try { const result = await api<ModelSettings>('/settings', { method: 'PUT', body: JSON.stringify({ model: model.trim(), base_url: baseUrl.trim(), ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}) }) }); setSettings(result); setBaseUrl(result.base_url); setModel(result.model); setApiKey(''); notify(serviceChanged && !result.has_api_key ? '服务地址已更新，旧密钥已清除。填写新密钥后即可使用模型。' : '模型配置已保存。'); await refresh(); }
    catch (error) { setError(errorText(error)); } finally { mutationInFlight.current = false; setSaving(false); }
  };
  const clearKey = async () => {
    if (!settings || mutationInFlight.current) return;
    mutationInFlight.current = true; requestVersion.current++; setLoading(false); setSaving(true); setError(''); setTest(null);
    try {
      const result = await api<ModelSettings>('/settings', { method: 'PUT', body: JSON.stringify({ model: settings.model, base_url: settings.base_url, clear_api_key: true }) });
      setSettings(result); setApiKey(''); notify('密钥已清除，后续请求将使用检索摘录模式。'); await refresh();
    } catch (error) { setError(errorText(error)); } finally { mutationInFlight.current = false; setSaving(false); }
  };
  const testConnection = async () => { const version = ++requestVersion.current; setTesting(true); setTest(null); setError(''); try { const result = await post<{ ok: boolean; message: string }>('/settings/test'); if (version === requestVersion.current) setTest(result); await refresh(); } catch (error) { if (version === requestVersion.current) setError(errorText(error)); } finally { setTesting(false); } };
  const changed = settings !== null && (model !== settings.model || baseUrl !== settings.base_url || apiKey.length > 0);
  return <div className="page settings-page"><div className="page-heading"><div><div className="eyebrow">MAKE IT YOURS</div><h1>模型设置<span className="title-period">.</span></h1><p>连接你使用的语言模型，为研究工作增添助手。</p></div></div><div className="settings-layout"><section className="settings-card"><div className="settings-card-heading"><span className="settings-icon"><Settings2 size={21} /></span><div><h2>语言模型连接</h2><p>支持兼容 OpenAI Chat Completions 的接口。</p></div></div>{error && <ErrorBox>{error}</ErrorBox>}{loading ? <div className="loading-area"><Spinner label="正在加载配置…" /></div> : <form onSubmit={event => void save(event)}><label className="form-label" htmlFor="model-base">API 地址</label><input id="model-base" className="form-input" type="url" required disabled={saving || testing} value={baseUrl} onChange={event => { setBaseUrl(event.target.value); setTest(null); }} placeholder="https://your-provider.example/v1" autoComplete="off" /><p className="field-hint">填写服务的基础地址，通常以 /v1 结尾。本地模型服务也可连接。</p><label className="form-label" htmlFor="model-name">模型名称</label><input id="model-name" className="form-input" required disabled={saving || testing} value={model} onChange={event => { setModel(event.target.value); setTest(null); }} placeholder="服务提供方支持的模型名称" autoComplete="off" /><label className="form-label" htmlFor="model-key">API Key <span>{settings?.has_api_key ? '已保存密钥' : '尚未设置'}</span></label><div className="password-input"><KeyRound size={16} /><input id="model-key" type="password" disabled={saving || testing} value={apiKey} onChange={event => { setApiKey(event.target.value); setTest(null); }} placeholder={serviceChanged ? '请输入新服务的密钥；留空会清除旧密钥' : settings?.has_api_key ? '留空以保留当前服务的密钥' : '输入 API Key；无鉴权本地服务填写 local'} autoComplete="new-password" /></div><p className="field-hint">密钥保存在本地后端。服务地址未变时留空保留；更换地址时需填写新密钥，留空会清除旧密钥。无鉴权本地服务可填写 local。</p>{serviceChanged && settings?.has_api_key && <Notice>服务地址已改变。保存时会清除旧服务的密钥；如需连接新服务，请填写它的密钥。</Notice>}<div className="settings-footer"><button className="button" type="button" disabled={saving || testing || changed || !settings?.has_api_key} onClick={() => void clearKey()}>清除密钥</button><button className="button" type="button" disabled={testing || saving || changed || !settings?.has_api_key} onClick={() => void testConnection()}>{testing ? <Spinner label="测试中" /> : <><Link2 size={15} />测试连接</>}</button><button className="button primary" disabled={saving || testing || !model.trim() || !baseUrl.trim()}>{saving ? <Spinner label="保存中" /> : <><Check size={15} />保存配置</>}</button></div>{changed && <p className="field-hint">请先保存变更，再测试连接。</p>}{test && <div className={`notice ${test.ok ? 'success' : 'error'}`} role="status"><CheckCircle2 size={17} /><span>{test.message}</span></div>}</form>}</section><aside className="settings-about"><div className="settings-about-icon"><ShieldCheck size={27} /></div><h3>清楚知道，助手在做什么</h3><div className="settings-state"><span className={`tiny-dot ${health?.model_configured ? 'green' : ''}`} /><strong>{health?.model_configured ? '模型已配置' : '检索摘录模式'}</strong></div><p>未配置模型时，上传、解析、阅读、检索摘录与图谱仍可使用。</p><p>接入模型后，可生成文献回答、翻译和研究笔记。发送给模型的内容将按任务包含选中文献的相关文本。</p><div className="subtle-divider" /><dl><div><dt>文档解析</dt><dd>{health?.parser || '等待服务连接'}</dd></div><div><dt>OCR 支持</dt><dd>{health ? health.ocr_available ? '可用' : '未检测到' : '待检测'}</dd></div><div><dt>请求超时</dt><dd>{settings ? `${settings.timeout_seconds} 秒` : '—'}</dd></div></dl></aside></div></div>;
}
