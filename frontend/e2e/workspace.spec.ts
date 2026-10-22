import { readFile } from 'node:fs/promises';
import { test, expect, cleanWorkspace, fixture, readyPaper, waitPaper, nav, selectPaper, question, goPage, assertHighlightVisible } from './helpers';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

test('uploads a real PDF, parses pages, skips identical bytes, and rejects a damaged file', async ({ page, request }) => {
  await page.goto('/');
  await expect(page.getByText('建立你的第一座文献库')).toBeVisible();
  const upload = page.locator('.library-page input[type=file]');
  await upload.setInputFiles(fixture());
  const row = page.locator('.paper-row');
  await expect(row).toHaveCount(1);
  await expect(row.locator('.status')).toHaveText('已就绪', { timeout: 20_000 });
  await expect(row.locator('.paper-title')).toHaveText('Deterministic Research Fixture');
  const { papers } = await (await request.get('/api/papers')).json();
  expect(papers[0].processed_pages).toBe(3);
  expect(papers[0].text_chars).toBeGreaterThan(300);
  await upload.setInputFiles(fixture('duplicate.pdf'));
  await expect(page.locator('.toast')).toContainText('1 个重复文件已跳过');
  expect((await (await request.get('/api/papers')).json()).papers).toHaveLength(1);
  const invalid = page.waitForResponse(response => response.url().endsWith('/api/papers/upload') && response.request().method() === 'POST');
  await upload.setInputFiles(fixture('broken.pdf'));
  expect((await invalid).status()).toBe(400);
  await expect(page.locator('.toast.error')).toBeVisible();
  expect((await (await request.get('/api/papers')).json()).papers).toHaveLength(1);
});

test('extractive chat saves its scope and evidence, opens the source, and survives a reload', async ({ page, request }) => {
  const paper = await readyPaper(request);
  await page.goto('/');
  await selectPaper(page);
  await nav(page, '文献问答');
  await page.getByRole('textbox', { name: '输入文献问题' }).fill(question);
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.locator('.message-assistant .mode-tag')).toContainText('检索摘录');
  const evidence = page.locator('.chat-page .source-card').filter({ hasText: '8 NVIDIA P100 GPUs' }).first();
  await expect(evidence).toContainText('第 2 页');
  await expect.poll(async () => {
    const { sessions } = await (await request.get('/api/sessions')).json();
    return sessions.length ? (await (await request.get(`/api/sessions/${sessions[0].id}`)).json()).messages.at(-1)?.status : '';
  }).toBe('completed');
  const { sessions } = await (await request.get('/api/sessions')).json();
  const saved = await (await request.get(`/api/sessions/${sessions[0].id}`)).json();
  expect(saved.messages[0].paper_ids).toEqual([paper.id]);
  expect(saved.messages[1].paper_ids).toEqual([paper.id]);
  expect(saved.messages[1].sources.some((source: { page: number; bbox: number[] }) => source.page === 2 && source.bbox.length === 4)).toBe(true);
  await evidence.click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('2');
  await assertHighlightVisible(page);
  await page.reload();
  await nav(page, '文献问答');
  await expect(page.locator('.message-user')).toContainText(question);
  await expect(page.locator('.message-assistant')).toContainText('8 NVIDIA P100 GPUs');
  expect((await (await request.get(`/api/sessions/${saved.id}`)).json()).messages).toEqual(saved.messages);
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect(page.locator('.chat-welcome')).toBeVisible();
  await page.locator('.chat-page .saved-list-item').filter({ hasText: saved.title }).getByRole('button').first().click();
  await expect(page.locator('.message-user')).toContainText(question);
  await page.getByRole('button', { name: `删除会话 ${saved.title}`, exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect.poll(async () => (await request.get(`/api/sessions/${saved.id}`)).status()).toBe(404);
});

test('research notes create immutable versions, reload, and export full evidence', async ({ page, request }) => {
  await readyPaper(request);
  await page.goto('/');
  await selectPaper(page);
  await nav(page, '研究写作');
  await page.getByRole('button', { name: '整理并保存摘录' }).click();
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 1');
  await page.getByRole('textbox', { name: '研究笔记标题' }).fill('E2E saved evidence note');
  await page.locator('.research-result').getByRole('button', { name: '编辑', exact: true }).click();
  const editor = page.getByRole('textbox', { name: '研究笔记正文' });
  const original = await editor.inputValue();
  await editor.fill(`${original}\n\nReview checkpoint: revised and verified.`);
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 2');
  const { notes } = await (await request.get('/api/notes')).json();
  const note = await (await request.get(`/api/notes/${notes[0].id}`)).json();
  expect(note.versions).toHaveLength(2);
  expect((await (await request.get(`/api/notes/${note.id}/versions/1`)).json()).content).toBe(original);
  await page.reload();
  await nav(page, '研究写作');
  await expect(page.getByRole('textbox', { name: '研究笔记标题' })).toHaveValue('E2E saved evidence note');
  await expect(page.locator('.note-preview')).toContainText('Review checkpoint: revised and verified.');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '完整 Markdown' }).click();
  const download = await downloadPromise;
  const markdown = await readFile((await download.path())!, 'utf8');
  expect(markdown).toContain('## 引用证据');
  expect(markdown).toContain('paper_id=');
  expect(markdown).toContain('/api/papers/');
  await page.getByRole('combobox', { name: '查看笔记历史版本' }).selectOption('1');
  await expect(editor).toHaveValue(original);
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 3');
  expect((await (await request.get(`/api/notes/${note.id}/versions/2`)).json()).content).toContain('Review checkpoint:');
  const restored = await (await request.get(`/api/notes/${note.id}`)).json();
  await page.getByRole('button', { name: `删除笔记 ${restored.title}`, exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect.poll(async () => (await request.get(`/api/notes/${note.id}`)).status()).toBe(404);
});

test('reader search, reference round trip, text selection and reading progress use real PDF coordinates', async ({ page, request }) => {
  const paper = await readyPaper(request);
  await page.goto('/');
  await page.locator('.paper-title').click();
  await page.getByRole('button', { name: '全文检索', exact: true }).click();
  await page.getByRole('textbox', { name: '全文检索关键词' }).fill('NVIDIA P100');
  await page.locator('.document-search').getByRole('button', { name: '搜索', exact: true }).click();
  await page.locator('.document-results > button').filter({ hasText: '8 NVIDIA P100' }).click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('2');
  await assertHighlightVisible(page);
  await page.getByRole('button', { name: '可选文字层' }).click();
  await expect(page.locator('.pdf-text-layer')).toContainText('NVIDIA');
  await goPage(page, 1);
  await page.getByRole('button', { name: '参考文献', exact: true }).click();
  await page.getByRole('button', { name: '[1] 跳转参考文献', exact: true }).click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('3');
  await assertHighlightVisible(page);
  await page.getByRole('button', { name: '返回引用处' }).click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('1');
  await goPage(page, 2);
  await page.locator('.paper-scroll').evaluate(pane => { pane.scrollTop = (pane.scrollHeight - pane.clientHeight) * 0.5; });
  await expect.poll(async () => {
    const position = await (await request.get(`/api/papers/${paper.id}/reading`)).json();
    return position.page === 2 && position.scroll_ratio > 0.4 && position.scroll_ratio < 0.6;
  }).toBe(true);
  await page.reload();
  await page.locator('.paper-title').click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('2');
  await expect.poll(() => page.locator('.paper-scroll').evaluate(pane => pane.scrollTop / Math.max(1, pane.scrollHeight - pane.clientHeight))).toBeGreaterThan(0.4);
  const unexpectedDownloads: string[] = [];
  page.on('download', download => unexpectedDownloads.push(download.suggestedFilename()));
  const failedExport = page.waitForResponse(response => response.url().includes('/translations/export'));
  await page.getByRole('button', { name: '译文 MD', exact: true }).click();
  expect((await failedExport).status()).toBe(409);
  await expect(page.locator('.toast.error')).toBeVisible();
  expect(unexpectedDownloads).toEqual([]);
  await page.getByRole('button', { name: '范围翻译', exact: true }).click();
  await page.getByRole('button', { name: '估计工作量' }).click();
  await expect(page.locator('.translation-estimate')).toContainText('3 页');
  await expect(page.getByRole('button', { name: '翻译为中文', exact: true })).toBeDisabled();
});

test('real parsing can pause, resume, cancel, and restart from the library', async ({ page, request }) => {
  await page.goto('/');
  await page.locator('.library-page input[type=file]').setInputFiles(fixture('slow.pdf'));
  await expect(page.locator('.paper-row')).toHaveCount(1);
  const { papers } = await (await request.get('/api/papers')).json();
  const id = papers[0].id;
  await page.locator('.job-item').getByRole('button', { name: '暂停', exact: true }).click();
  await waitPaper(request, id, 'paused');
  await page.getByRole('button', { name: '刷新文献', exact: true }).click();
  await expect(page.locator('.paper-row .status')).toHaveText('解析已暂停');
  await page.getByRole('button', { name: /后台任务/ }).click();
  await page.locator('.job-item').getByRole('button', { name: '恢复', exact: true }).click();
  await page.locator('.job-item').getByRole('button', { name: '取消', exact: true }).click();
  await waitPaper(request, id, 'cancelled');
  await page.getByRole('button', { name: '刷新文献', exact: true }).click();
  await expect(page.locator('.paper-row .status')).toHaveText('解析已取消');
  await page.locator('.paper-row').getByLabel('更多操作', { exact: true }).click();
  await page.getByRole('button', { name: '重新解析', exact: true }).click();
  await waitPaper(request, id, 'ready');
  await page.getByRole('button', { name: '刷新文献', exact: true }).click();
  await expect(page.locator('.paper-row .status')).toHaveText('已就绪');
  expect((await (await request.get(`/api/papers/${id}`)).json()).paper.processed_pages).toBe(48);
});

test('stopping a delayed real chat persists the error and retry completes without overwriting another session', async ({ page, request }) => {
  const paper = await readyPaper(request);
  await page.goto('/');
  await selectPaper(page);
  await nav(page, '文献问答');
  await page.route('**/api/chat', route => route.continue({ headers: { ...route.request().headers(), 'x-e2e-delay': 'yes' } }), { times: 1 });
  const started = page.waitForRequest(value => value.url().endsWith('/api/chat'));
  await page.getByRole('textbox', { name: '输入文献问题' }).fill(question);
  await page.getByRole('button', { name: '发送问题' }).click();
  await started;
  await page.getByRole('button', { name: '停止', exact: true }).click();
  await expect(page.locator('.message-assistant')).toContainText('本次生成已停止');
  const { sessions } = await (await request.get('/api/sessions')).json();
  const originalId = sessions[0].id;
  await expect.poll(async () => (await (await request.get(`/api/sessions/${originalId}`)).json()).messages.at(-1).status).toBe('error');
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect(page.locator('.chat-welcome')).toBeVisible();
  // Wait past the server's bounded delay with a real request. Its result is
  // deliberately not injected into the UI, so late-response protection is checked.
  const settled = await request.post('/api/chat', { headers: { 'x-e2e-delay': 'yes' }, data: { question, paper_ids: [paper.id], history: [] } });
  expect(settled.ok()).toBeTruthy();
  await expect(page.locator('.message-assistant')).toHaveCount(0);
  expect((await (await request.get(`/api/sessions/${originalId}`)).json()).messages.at(-1).status).toBe('error');
  const summaries = (await (await request.get('/api/sessions')).json()).sessions;
  const original = summaries.find((item: {id: string}) => item.id === originalId);
  await page.locator('.chat-page .saved-list-item').filter({ hasText: original.title }).getByRole('button').first().click();
  await page.getByRole('button', { name: '重试这次问题', exact: true }).click();
  await expect(page.locator('.message-assistant .mode-tag')).toContainText('检索摘录');
  await expect.poll(async () => (await (await request.get(`/api/sessions/${originalId}`)).json()).messages.at(-1).status).toBe('completed');
});

test.describe('mobile layout', () => {
  // Responsive viewport and touch inputs work in all three engines. Firefox
  // does not implement Playwright's isMobile device emulation.
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  test('opens a real PDF and switches between library and saved chat without horizontal overflow', async ({ page, request }) => {
    await readyPaper(request);
    await page.goto('/');
    await expect(page.locator('.paper-title')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.locator('.paper-title').tap();
    await expect(page.locator('.paper-image')).toBeVisible();
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('2');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await nav(page, '文献库');
    await selectPaper(page);
    await nav(page, '文献问答');
    await page.getByRole('textbox', { name: '输入文献问题' }).fill(question);
    await page.getByRole('button', { name: '发送问题' }).click();
    await expect(page.locator('.message-assistant .mode-tag')).toContainText('检索摘录');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.reload();
    await nav(page, '文献问答');
    await expect(page.locator('.message-assistant')).toContainText('8 NVIDIA P100 GPUs');
  });
});
