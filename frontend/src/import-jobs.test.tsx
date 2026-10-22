// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import Library from './Library';
import { WorkspaceProvider } from './store';
import type { Job, JobListing, Paper } from './api';

const paper: Paper = {id:'p',title:'Uploaded fixture',filename:'good.pdf',authors:[],abstract:'',arxiv_id:null,version:null,family_id:'p',page_count:1,processed_pages:1,status:'ready',error:null,created_at:'',tags:[],is_favorite:false,duplicate_of:null,warnings:[],source:'upload',text_chars:30,ocr_pages:0};
const job = (id:string,status='done'): Job => ({id,kind:'parse',paper_id:'p',status,progress:status==='done'?100:10,message:`Job ${id}`,created_at:id,updated_at:id});
const json = (value:unknown,status=200) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
let papers:Paper[], active:Job[], history:Job[], paperReads:number;
let uploadResponse:() => Promise<Response>;
let holdFirstPapers:(() => Promise<Response>) | null;
let calls:string[];

function listing(cursor:string | null): JobListing {
  const offset=cursor==='older'?20:0;
  const rows=history.slice(offset,offset+20);
  return {jobs:[...active,...rows],active_jobs:active,history_jobs:rows,next_cursor:offset+20<history.length?'older':null,history_total:history.length};
}
beforeEach(()=>{
  papers=[];active=[];history=[];paperReads=0;calls=[];holdFirstPapers=null;
  uploadResponse=async()=>json({papers:[],duplicates:[],errors:[],status:'ok'});
  HTMLElement.prototype.scrollIntoView=vi.fn();
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo | URL,init?:RequestInit)=>{
    const url=new URL(String(input),'http://localhost');calls.push(url.pathname+url.search);
    if(url.pathname==='/api/papers/upload')return uploadResponse();
    if(url.pathname==='/api/papers') {paperReads++;if(paperReads===1&&holdFirstPapers)return holdFirstPapers();return json({papers});}
    if(url.pathname==='/api/jobs')return json(listing(url.searchParams.get('cursor')));
    if(url.pathname.endsWith('/resume')&&init?.method==='POST') {const id=url.pathname.split('/')[3];active=active.map(item=>item.id===id?{...item,status:'queued'}:item);return json(active.find(item=>item.id===id));}
    if(url.pathname==='/api/health')return json({status:'ok',model_configured:false,model:'',ocr_available:false,parser:'test'});
    if(url.pathname==='/api/stats')return json({papers:papers.length,ready:papers.length,pages:papers.length,processing:0,families:papers.length});
    throw new Error(`Unexpected API ${url.pathname}`);
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
const mount=()=>render(<WorkspaceProvider><Library/></WorkspaceProvider>);

it('shows each partial upload error and queues a fresh read after an older GET finishes',async()=>{
  let release!:(value:Response)=>void;
  holdFirstPapers=()=>new Promise(resolve=>{release=resolve;});
  uploadResponse=async()=>{papers=[paper];return json({papers:[paper],duplicates:[],status:'partial',errors:[{filename:'broken.pdf',status:400,detail:'不是有效 PDF'}]},207);};
  const {container}=mount();
  await waitFor(()=>expect(release).toBeDefined());
  fireEvent.change(container.querySelector('input[type="file"]')!,{target:{files:[new File(['ok'],'good.pdf',{type:'application/pdf'}),new File(['bad'],'broken.pdf',{type:'application/pdf'})]}});
  await screen.findByText('broken.pdf');
  expect(paperReads).toBe(1);
  // This request intentionally returns the pre-upload snapshot late.
  await act(async()=>release(json({papers:[]})));
  await screen.findByRole('button',{name:'Uploaded fixture'});
  await waitFor(()=>expect(paperReads).toBe(2));
  expect(screen.getByText(/已接收 1 篇，跳过 0 个重复文件，1 个文件失败/)).toBeTruthy();
  expect(screen.getByText(/成功文件已经保存/)).toBeTruthy();
});

it('keeps all-failed per-file diagnostics and refreshes even for a non-success response',async()=>{
  uploadResponse=async()=>json({papers:[],duplicates:[],status:'failed',detail:'两个文件导入失败',errors:[{filename:'broken-a.pdf',detail:'无法打开',status:400},{filename:'broken-b.pdf',detail:'不是有效 PDF',status:400}]},400);
  const {container}=mount();
  await screen.findByText('建立你的第一座文献库');
  fireEvent.change(container.querySelector('input[type="file"]')!,{target:{files:[new File(['bad'],'broken-a.pdf',{type:'application/pdf'})]}});
  await screen.findByText('broken-a.pdf');
  expect(screen.getByText('broken-b.pdf')).toBeTruthy();
  await waitFor(()=>expect(paperReads).toBe(2));
  expect(screen.getByText(/已接收 0 篇，跳过 0 个重复文件，2 个文件失败/)).toBeTruthy();
});

it('renders every actionable job, resumes an old pause, and pages terminal history',async()=>{
  papers=[paper];
  active=Array.from({length:105},(_,i)=>job(`active-${i}`,'paused'));
  history=Array.from({length:25},(_,i)=>job(`history-${i}`));
  mount();
  await screen.findByText('Job active-104');
  expect(screen.getAllByRole('button',{name:'恢复'})).toHaveLength(105);
  const row=screen.getByText('Job active-104').closest('.job-item') as HTMLElement;
  fireEvent.click(within(row).getByRole('button',{name:'恢复'}));
  await waitFor(()=>expect(calls).toContain('/api/jobs/active-104/resume'));
  await waitFor(()=>expect(within(row).queryByRole('button',{name:'恢复'})).toBeNull());
  fireEvent.click(screen.getByRole('button',{name:/后台任务/}));
  await screen.findByText('Job history-0');
  expect(screen.queryByText('Job history-20')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'下一页历史'}));
  await screen.findByText('Job history-24');
  expect(screen.queryByText('Job history-0')).toBeNull();
  expect(screen.getByText('Job active-104')).toBeTruthy();
  expect((screen.getByRole('button',{name:'下一页历史'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'上一页历史'}));
  await screen.findByText('Job history-0');
  expect(calls).toContain('/api/jobs?limit=20&cursor=older');
});
