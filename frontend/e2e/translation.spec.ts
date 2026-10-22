import { test, expect, cleanWorkspace, readyPaper, nav, goPage, waitPaper, assertHighlightVisible, screenshotPath } from './helpers';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

test('sentence pairs use real sources, a local model protocol stub, and a durable cache', async ({ page, request, baseURL }) => {
  const paper = await readyPaper(request);
  await page.goto('/');
  await page.locator('.settings-nav').click();
  await page.getByLabel('API 地址').fill(`${baseURL}/__e2e__/model`);
  await page.getByLabel('模型名称').fill('e2e-contract-model');
  await page.locator('#model-key').fill('e2e-contract-only');
  await page.getByRole('button', {name:'保存配置',exact:true}).click();
  await expect.poll(async () => (await (await request.get('/api/health')).json()).model_configured).toBe(true);
  await nav(page, '文献库'); await page.locator('.paper-title').click();
  await page.getByRole('button', {name:'句段对照',exact:true}).click();
  await expect(page.locator('.aligned-pair')).not.toHaveCount(0);
  await page.getByRole('button', {name:'生成本页句段译文',exact:true}).click();
  await expect(page.locator('.aligned-target').first()).toContainText('测试服务译文：');
  const alignment = await (await request.get(`/api/papers/${paper.id}/pages/1/aligned-translation`)).json();
  expect(alignment.cached).toBe(true);
  for (const segment of alignment.segments) expect(segment.translation).toBe('测试服务译文：'+segment.source);
  await page.getByLabel('定位译文片段 1', {exact:true}).click(); await assertHighlightVisible(page);
  await page.screenshot({path:screenshotPath('translation-desktop')});
  await page.reload(); await page.locator('.paper-title').click();
  await page.getByRole('button', {name:'句段对照',exact:true}).click();
  await expect(page.locator('.aligned-target').first()).toContainText('测试服务译文：');
  await goPage(page, 2);
  await expect(page.locator('.aligned-target').first()).toContainText('尚未生成译文');
  await page.getByRole('button', {name:'段落',exact:true}).click();
  const source = page.locator('.text-block-source').filter({hasText:'calibration'}).first();
  await source.evaluate(element => {
    const range = document.createRange(); range.selectNodeContents(element);
    const selection = window.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
    element.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
  });
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  await page.getByRole('button', {name:'翻译选中文字',exact:true}).click();
  await expect(page.locator('.selected-translation')).toContainText('测试服务译文：');
  await goPage(page, 3); await expect(page.locator('.selection-translation')).toHaveCount(0);
});

test('sentence source pairs remain usable at mobile width without a model', async ({ page, request }) => {
  await page.setViewportSize({width:390,height:844});
  await readyPaper(request); await page.goto('/'); await page.locator('.paper-title').click();
  await page.getByRole('button',{name:'句段对照',exact:true}).click();
  await expect(page.locator('.aligned-pair')).not.toHaveCount(0);
  await expect(page.getByRole('button',{name:'配置模型后翻译',exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
  await page.getByLabel('定位原文片段 1',{exact:true}).click(); await assertHighlightVisible(page);
  await page.screenshot({path:screenshotPath('translation-mobile')});
});

test('rebuilding parsed pages keeps the PDF identity and saved notes', async ({ page, request }) => {
  const paper = await readyPaper(request);
  const note = await request.post('/api/notes', {data:{title:'Retained research',content:'Keep this analysis',paper_ids:[paper.id],sources:[],kind:'note'}});
  expect(note.ok()).toBeTruthy(); const saved = await note.json();
  await page.goto('/');
  await page.locator('.paper-row').getByLabel('更多操作',{exact:true}).click();
  await page.getByRole('button',{name:'从首页重建解析',exact:true}).click();
  await waitPaper(request, paper.id, 'ready');
  const {papers} = await (await request.get('/api/papers')).json();
  expect(papers).toHaveLength(1); expect(papers[0].id).toBe(paper.id);
  expect((await (await request.get(`/api/notes/${saved.id}`)).json()).content).toBe('Keep this analysis');
});
