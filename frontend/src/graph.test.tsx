// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Graph } from './Tools';
const { openPaper } = vi.hoisted(() => ({openPaper: vi.fn()}));
vi.mock('./store', () => ({useWorkspace: () => ({view:'graph',revision:0,openPaper})}));
vi.mock('./api', async original => ({...await original<typeof import('./api')>(), api: vi.fn(async () => ({
  nodes:[{id:'latest',title:'Source latest version',citations:0},{id:'target',title:'Referenced paper',citations:1}],
  edges:[{source:'latest',target:'target',confidence:1,evidence:[{
    source_paper_id:'original-v1',source_version:1,page:5,block_id:'reference-block',bbox:[10,20,30,40],
    text:'Referenced paper. arXiv:1706.03762v1.',linked_paper_id:'target',linked_version:1,
    match_rule:'arxiv_id_and_version_exact',match_note:'按原文指定版本精确匹配。'
  }]}],scope:'Local corpus only'
}))}));
afterEach(() => {cleanup();vi.clearAllMocks();});
it('opens the actual cited source version and geometry behind a merged graph node', async () => {
  render(<Graph/>);
  fireEvent.click(await screen.findByRole('button',{name:'查看 Source latest version'}));
  fireEvent.click(screen.getByText('查看引用依据（1）'));
  expect(screen.getByText('按原文指定版本精确匹配。')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'定位引用原文 · 第 5 页 · v1'}));
  expect(openPaper).toHaveBeenCalledWith('original-v1',5,expect.objectContaining({
    id:'reference-block',paper_id:'original-v1',page:5,bbox:[10,20,30,40]
  }));
});
