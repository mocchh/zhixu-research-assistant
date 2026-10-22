import { cleanWorkspace, expect, nav, question, readyPaper, selectPaper, test } from './helpers';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

test('note drafts survive cancelled navigation and typing during a delayed real save', async ({ page, request }) => {
  const response = await request.post('/api/notes', { data: { title: 'Draft safety', content: 'Original text', paper_ids: [], sources: [], kind: 'note' } });
  const note = await response.json();
  await page.goto('/'); await nav(page, '研究写作');
  await expect(page.getByLabel('研究笔记标题')).toHaveValue('Draft safety');
  await page.locator('.research-result').getByRole('button', { name: '编辑', exact: true }).click();
  const editor = page.getByLabel('研究笔记正文'); await editor.fill('Unsent manuscript');
  await page.getByRole('button', { name: '新笔记', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible(); await page.getByRole('button', { name: '返回保留草稿' }).click();
  await expect(editor).toHaveValue('Unsent manuscript');
  let release!: () => void; const responseGate = new Promise<void>(resolve => { release = resolve; }); let writeFinished = false;
  await page.route(`**/api/notes/${note.id}`, async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    const result = await route.fetch(); writeFinished = true; await responseGate; await route.fulfill({ response: result });
  });
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect.poll(() => writeFinished).toBe(true);
  await editor.fill('New text typed after saving started'); release();
  await expect(page.getByRole('button', { name: '保存版本', exact: true })).toBeEnabled();
  await expect(editor).toHaveValue('New text typed after saving started');
  expect((await (await request.get(`/api/notes/${note.id}`)).json()).content).toBe('Unsent manuscript');
  await page.unroute(`**/api/notes/${note.id}`);
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 3');
  await page.reload(); await nav(page, '研究写作'); await expect(page.locator('.note-preview')).toContainText('New text typed after saving started');
});

test('a failed chat save retains evidence across reopening and retries without a second chat request', async ({ page, request }) => {
  await readyPaper(request); await page.goto('/'); await selectPaper(page); await nav(page, '文献问答');
  let chatCalls = 0; let failSave = true;
  page.on('request', req => { if (new URL(req.url()).pathname === '/api/chat') chatCalls++; });
  await page.route('**/api/sessions/*', async route => {
    if (route.request().method() === 'PUT') {
      const data = route.request().postDataJSON();
      if (failSave && data.messages.some((m: { role: string; status: string }) => m.role === 'assistant' && m.status === 'completed')) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Simulated unavailable storage' }) });
    }
    return route.continue();
  });
  await page.getByLabel('输入文献问题').fill(question); await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.getByRole('button', { name: '重试保存回答' })).toBeEnabled();
  const answer = await page.locator('.answer-content').innerText();
  const { sessions } = await (await request.get('/api/sessions')).json(); const id = sessions[0].id;
  expect((await (await request.get(`/api/sessions/${id}`)).json()).messages.at(-1).status).toBe('pending');
  await page.locator('.chat-page .saved-list-item').first().getByRole('button').first().click();
  await expect(page.locator('.answer-content')).toHaveText(answer, { useInnerText: true });
  await expect(page.locator('.source-card').first()).toBeVisible();
  failSave = false; await page.getByRole('button', { name: '重试保存回答' }).click();
  await expect.poll(async () => (await (await request.get(`/api/sessions/${id}`)).json()).messages.at(-1).status).toBe('completed');
  await expect(page.getByRole('button', { name: '重试保存回答' })).toHaveCount(0); expect(chatCalls).toBe(1);
  await page.reload(); await nav(page, '文献问答'); await expect(page.locator('.answer-content')).toHaveText(answer, { useInnerText: true }); expect(chatCalls).toBe(1);
});
