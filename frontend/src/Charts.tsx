import { useRef, useState, type FormEvent } from 'react';
import { ArrowDownToLine, BarChart3, FileSpreadsheet, LineChart, Upload } from 'lucide-react';
import { api, errorText } from './api';
import { ErrorBox, Notice, Spinner } from './ui';

interface ChartResult { columns: string[]; numeric_columns: string[]; rows: number; preview: Record<string, unknown>[]; svg: string; png_base64: string; caption: string; warnings: string[] }
export default function Charts() {
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<'line' | 'bar'>('line');
  const [x, setX] = useState('');
  const [ys, setYs] = useState<string[]>([]);
  const [result, setResult] = useState<ChartResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const generate = async (currentFile: File, currentX = x, currentYs = ys) => {
    setBusy(true); setError('');
    try {
      const form = new FormData(); form.append('file', currentFile); form.append('kind', kind);
      if (currentX) form.append('x', currentX);
      if (currentYs.length) form.append('y', currentYs.join(','));
      const next = await api<ChartResult>('/charts', { method: 'POST', body: form });
      setResult(next);
      if (!currentX && next.columns.length) setX(next.columns[0]);
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  };
  const choose = (nextFile: File) => { setFile(nextFile); setResult(null); setX(''); setYs([]); void generate(nextFile, '', []); };
  const submit = (event: FormEvent) => { event.preventDefault(); if (file && !busy) void generate(file); };
  return <section className="charts-section"><div className="charts-heading"><span className="chart-heading-icon"><BarChart3 size={21} /></span><div><h2>从实验数据，到论文图表</h2><p>上传真实 CSV，生成可下载的图表与基于统计的图注。</p></div><span className="chart-local-badge">无需模型</span></div><form onSubmit={submit}><div className="chart-upload"><div><FileSpreadsheet size={21} /><span><strong>{file?.name || '选择实验数据 CSV'}</strong><small>{file ? `${(file.size / 1024).toFixed(1)} KB${result ? ` · ${result.rows} 行数据` : ''}` : '支持数值列比较、趋势折线和柱状图'}</small></span></div><button type="button" className="button small" disabled={busy} onClick={() => input.current?.click()}><Upload size={14} />{file ? '更换文件' : '上传 CSV'}</button><input ref={input} type="file" hidden accept=".csv,text/csv" onChange={event => { const nextFile = event.target.files?.[0]; if (nextFile) choose(nextFile); event.target.value = ''; }} /></div>{result && <div className="chart-controls"><div><label className="form-label">图表类型</label><div className="chart-type"><button type="button" className={kind === 'line' ? 'active' : ''} onClick={() => setKind('line')}><LineChart size={15} />折线图</button><button type="button" className={kind === 'bar' ? 'active' : ''} onClick={() => setKind('bar')}><BarChart3 size={15} />柱状图</button></div></div><div><label className="form-label" htmlFor="chart-x">横轴列</label><select id="chart-x" value={x} onChange={event => { setX(event.target.value); setYs(previous => previous.filter(y => y !== event.target.value)); }}>{result.columns.map(column => <option key={column} value={column}>{column}</option>)}</select></div><div className="chart-y-selector"><label className="form-label">数值列 <span>最多 3 列；未选时自动选择</span></label><div>{result.numeric_columns.filter(column => column !== x).map(column => <label key={column}><input type="checkbox" checked={ys.includes(column)} disabled={!ys.includes(column) && ys.length >= 3} onChange={() => setYs(previous => previous.includes(column) ? previous.filter(y => y !== column) : [...previous, column])} />{column}</label>)}</div></div><button className="button primary small" disabled={busy || !file}>{busy ? <Spinner label="绘图中" /> : '更新图表'}</button></div>}</form>{error && <ErrorBox>{error}</ErrorBox>}{busy && !result && <div className="loading-area"><Spinner label="正在读取数据并绘制图表…" /></div>}{result && <div className="chart-result">{result.warnings?.map((warning, index) => <Notice key={index}>{warning}</Notice>)}<div className="chart-image"><img src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(result.svg)}`} alt={result.caption || '基于所上传 CSV 生成的实验图表'} /></div><div className="chart-caption"><strong>Figure caption</strong><p>{result.caption}</p></div><div className="chart-downloads"><a className="button small" href={`data:image/png;base64,${result.png_base64}`} download={`${file?.name.replace(/\.csv$/i, '') || 'research'}-chart.png`}><ArrowDownToLine size={14} />下载 PNG</a><a className="button small" href={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(result.svg)}`} download={`${file?.name.replace(/\.csv$/i, '') || 'research'}-chart.svg`}><ArrowDownToLine size={14} />下载 SVG</a></div><details className="chart-preview"><summary>查看数据预览 · {result.rows} 行 / {result.columns.length} 列</summary><div><table><thead><tr>{result.columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.preview.map((row, index) => <tr key={index}>{result.columns.map(column => <td key={column}>{row[column] === null || row[column] === undefined ? '—' : String(row[column])}</td>)}</tr>)}</tbody></table></div></details></div>}</section>;
}
