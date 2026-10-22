// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createRef } from 'react';
import AlignedTranslation from './AlignedTranslation';
import SelectionTranslation, { selectedSource } from './SelectionTranslation';
import { api, post } from './api';
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: vi.fn(), post: vi.fn() }));
vi.mock('./ui', () => ({ ErrorBox: ({children}: {children: React.ReactNode}) => <div role="alert">{children}</div>, Spinner: ({label}: {label:string}) => <span>{label}</span>, Markdown: ({text}: {text:string}) => <p>{text}</p> }));
const value = (paper='p') => ({paper_id:paper,page:1,target_language:'zh',source_hash:'hash',cached:false,notes:[],segments:[{id:'s1',block_id:'b1',page:1,start:0,end:12,source:`${paper} original.`,bbox:[1,2,3,4],kind:'paragraph',translation:null}]});
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockImplementation(async path => value(path.includes('/q/') ? 'q' : 'p') as never); });
afterEach(() => {cleanup();window.getSelection()?.removeAllRanges();});
it('both original and translated sides locate the same real block', async () => {
  const focus = vi.fn(); vi.mocked(api).mockResolvedValue({...value(),segments:[{...value().segments[0],translation:'真实译文'}]});
  render(<AlignedTranslation paperId="p" page={1} language="zh" configured configure={vi.fn()} focus={focus}/>);
  fireEvent.click(await screen.findByLabelText('定位原文片段 1')); fireEvent.click(screen.getByLabelText('定位译文片段 1'));
  expect(focus).toHaveBeenCalledTimes(2); expect(focus.mock.calls[0][0]).toMatchObject({id:'b1',paper_id:'p',page:1,bbox:[1,2,3,4]});
  expect(focus.mock.calls[1][0]).toEqual(focus.mock.calls[0][0]);
});
it('does not display a late translation after changing papers', async () => {
  let resolve!:(value:unknown)=>void; vi.mocked(post).mockImplementation(()=>new Promise(done=>{resolve=done;}) as never);
  const props = {page:1,language:'zh' as const,configured:true,configure:vi.fn(),focus:vi.fn()};
  const view = render(<AlignedTranslation {...props} paperId="p"/>);
  fireEvent.click(await screen.findByText('生成本页句段译文'));
  await waitFor(()=>expect(resolve).toBeDefined());
  view.rerender(<AlignedTranslation {...props} paperId="q"/>); await screen.findByText('q original.');
  resolve({...value(),segments:[{...value().segments[0],translation:'STALE TRANSLATION'}]});
  await waitFor(()=>expect(screen.queryByText('STALE TRANSLATION')).toBeNull());
  expect(screen.getByText('q original.')).toBeTruthy();
});
function select(node: Node, start=0, end=node.textContent!.length) {
  const range = document.createRange(); range.setStart(node,start);range.setEnd(node,end);
  const selection=window.getSelection()!;selection.removeAllRanges();selection.addRange(range);
}
it('only captures original text inside this reader', () => {
  const {container}=render(<div><main><p className="text-block-source">original source</p><p>unrelated controls</p></main><p className="text-block-source">other paper</p></div>);
  const root=container.querySelector('main')!;
  select(root.querySelector('.text-block-source')!.firstChild!,0,8); expect(selectedSource(root)).toBe('original');
  select(root.querySelectorAll('p')[1].firstChild!); expect(selectedSource(root)).toBe('');
  select(container.querySelectorAll('.text-block-source')[1].firstChild!);expect(selectedSource(root)).toBe('');
});
it('sends exact selected text and clears its result on page change', async () => {
  const root=createRef<HTMLElement>(); const configure=vi.fn();
  vi.mocked(post).mockResolvedValue({translation:'选文译文'});
  const View=({page}:{page:number})=><main ref={root}><p className="text-block-source">original source</p><SelectionTranslation paperId="p" page={page} language="zh" root={root} configured configure={configure}/></main>;
  const view=render(<View page={1}/>);
  select(screen.getByText('original source').firstChild!,0,8);fireEvent.mouseUp(screen.getByText('original source'));
  window.getSelection()?.removeAllRanges();
  fireEvent.click(await screen.findByText('翻译选中文字')); await screen.findByText('选文译文');
  expect(post).toHaveBeenCalledWith('/translate',{paper_id:'p',page:1,text:'original',target_language:'zh'},expect.objectContaining({signal:expect.any(AbortSignal)}));
  view.rerender(<View page={2}/>);expect(screen.queryByText('选文译文')).toBeNull();expect(screen.queryByText('翻译选中文字')).toBeNull();
});
