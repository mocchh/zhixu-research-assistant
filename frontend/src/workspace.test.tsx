// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session, Note } from './persistence';
const testState = vi.hoisted(() => ({ sessions: {} as Record<string, Session>, notes: {} as Record<string, Note>, chat: null as null | ((body: unknown) => Promise<unknown>), research: null as null | (() => Promise<unknown>), selected: ['paper-a'], setSelected: vi.fn(), notify: vi.fn() }));
vi.mock('./store', () => ({ useWorkspace: () => ({selected:testState.selected,setSelected:testState.setSelected,papers:[],health:{model_configured:true,model:'test-model'},view:'chat',notify:testState.notify,setView:vi.fn()}) }));
vi.mock('./Charts', () => ({default:()=>null}));
vi.mock('./ui', () => ({
  AnswerView:({result}:{result:{answer:string}})=><p>{result.answer}</p>, ErrorBox:({children}:{children:React.ReactNode})=><div role="alert">{children}</div>, ModelHint:()=>null, SelectionScope:()=>null, Spinner:({label}:{label:string})=><span>{label}</span>, Modal:({children}:{children:React.ReactNode})=><div>{children}</div>, Markdown:({text}:{text:string})=><p>{text}</p>, Notice:({children}:{children:React.ReactNode})=><div>{children}</div>, SourceCards:()=>null,
}));
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>();
  const api = vi.fn(async (path:string, options:RequestInit={}) => {
    if (path==='/sessions') return {sessions:Object.values(testState.sessions)};
    if (path.startsWith('/sessions/')) { const id=path.split('/')[2]; if(options.method==='PUT'){ const body=JSON.parse(options.body as string); const next={...testState.sessions[id],...body,revision:body.revision+1}; testState.sessions[id]=structuredClone(next); return structuredClone(next); } return structuredClone(testState.sessions[id]); }
    if (path==='/notes') return {notes:Object.values(testState.notes)};
    if(path.startsWith('/notes/')) return structuredClone(testState.notes[path.split('/')[2]]);
    throw new Error(`Unexpected test API ${path}`);
  });
  return {...original,api,post:vi.fn(async(path:string,body:unknown)=>{if(path==='/chat')return testState.chat!(body);if(path==='/research')return testState.research!();throw new Error(`Unexpected test POST ${path}`);})};
});
import Chat from './Chat';
import Research, { noteMarkdown } from './Research';
beforeEach(()=>{
  HTMLElement.prototype.scrollIntoView=vi.fn();
  testState.sessions={a:{id:'a',title:'Conversation A',paper_ids:['paper-a'],messages:[],revision:1,created_at:'',updated_at:''},b:{id:'b',title:'Conversation B',paper_ids:['paper-b'],messages:[],revision:1,created_at:'',updated_at:''}};
  testState.notes={};testState.selected=['paper-a'];testState.setSelected.mockClear();
});
afterEach(()=>cleanup());
describe('conversation UI persistence',()=>{
  it('saves scope and evidence and reloads them after unmount',async()=>{
    testState.chat=async()=>({answer:'A durable answer',sources:[{id:'e',paper_id:'paper-a',page:7,text:'proof',bbox:[1,2,3,4]}],warnings:[],mode:'model'});
    const first=render(<Chat/>); await screen.findByRole('button',{name:'Conversation A'}); await waitFor(()=>expect(testState.setSelected).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('输入文献问题'),{target:{value:'GPU question'}});fireEvent.click(screen.getByLabelText('发送问题'));
    await screen.findByText('A durable answer'); await waitFor(()=>expect(testState.sessions.a.messages[1].status).toBe('completed'));
    expect(testState.sessions.a.messages[1].paper_ids).toEqual(['paper-a']);expect(testState.sessions.a.messages[1].sources[0].page).toBe(7);
    first.unmount();render(<Chat/>);await screen.findByText('A durable answer');
  });
  it('never puts a late answer into the newly selected conversation',async()=>{
    let resolve!:(result:unknown)=>void;testState.chat=()=>new Promise(done=>{resolve=done;});render(<Chat/>);
    await waitFor(()=>expect(testState.setSelected).toHaveBeenCalled());fireEvent.change(screen.getByLabelText('输入文献问题'),{target:{value:'Old request'}});fireEvent.click(screen.getByLabelText('发送问题'));
    await waitFor(()=>expect(resolve).toBeDefined());fireEvent.click(screen.getByRole('button',{name:'Conversation B'}));await waitFor(()=>expect(testState.setSelected).toHaveBeenCalledWith(['paper-b']));
    resolve({answer:'LATE RESULT FROM A',sources:[],mode:'model',warnings:[]});await waitFor(()=>expect(testState.sessions.a.messages[1].status).toBe('error'));
    expect(screen.queryByText('LATE RESULT FROM A')).toBeNull();expect(testState.sessions.b.messages).toHaveLength(0);
  });
});
describe('note export provenance',()=>{
  it('includes real identifiers, version, locator, and quoted evidence',()=>{
    const text=noteMarkdown('Note','Claim',[{id:'block-7',paper_id:'p',paper_title:'Transformer',page:7,text:'8 P100 GPUs',bbox:[1,2,3,4],score:1,arxiv_id:'1706.03762',version:7,authors:['A Author']} as never],'http://localhost:8000');
    expect(text).toContain('arXiv:1706.03762');expect(text).toContain('v7');expect(text).toContain('/api/papers/p/file#page=7');expect(text).toContain('block_id=block-7');expect(text).toContain('> 8 P100 GPUs');
  });
  it('keeps a previous research result when regeneration fails',async()=>{
    // With no saved note, create a local draft first, then fail generation.
    testState.research=async()=>{throw new Error('Model unavailable')};render(<Research/>);
    fireEvent.click(screen.getByRole('button',{name:'新笔记'}));fireEvent.change(screen.getByLabelText('研究笔记正文'),{target:{value:'Keep this carefully edited analysis'}});
    fireEvent.click(screen.getByRole('button',{name:'生成并保存笔记'}));await screen.findByText(/Model unavailable/);
    expect((screen.getByLabelText('研究笔记正文') as HTMLTextAreaElement).value).toBe('Keep this carefully edited analysis');
  });
});
