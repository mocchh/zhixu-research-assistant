import { readFile } from 'node:fs/promises';
import { type Page } from '@playwright/test';
import { cleanWorkspace, expect, nav, question, readyPaper, screenshotPath, test } from './helpers';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

async function openNotes(page: Page) {
  await page.goto('/');
  await nav(page, '研究写作');
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 1');
  await page.locator('.research-result').getByRole('button', { name: '编辑', exact: true }).click();
}

async function cancelUnsavedClose(page: Page) {
  const dialogPromise = page.waitForEvent('dialog', { timeout: 10_000 });
  await Promise.all([
    page.close({ runBeforeUnload: true }),
    dialogPromise.then(async dialog => {
      expect(dialog.type()).toBe('beforeunload');
      await dialog.dismiss();
    }),
  ]);
  expect(page.isClosed()).toBe(false);
}

test('two note windows reject a stale revision and preserve both edits through export and save-as', async ({ page, context, request }) => {
  const response = await request.post('/api/notes', { data: { title: 'Shared note', content: 'Original evidence', paper_ids: [], sources: [], kind: 'note' } });
  expect(response.ok()).toBeTruthy();
  const note = await response.json();
  const second = await context.newPage();
  await openNotes(page);
  await openNotes(second);
  await page.getByLabel('研究笔记正文').fill('Window A: independently written finding.');
  await second.getByLabel('研究笔记正文').fill('Window B: first committed finding.');
  await second.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect(second.locator('.research-result-heading')).toContainText('已保存 · 版本 2');
  const conflict = page.waitForResponse(value => value.url().endsWith(`/api/notes/${note.id}`) && value.request().method() === 'PUT');
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  expect((await conflict).status()).toBe(409);
  await expect(page.getByLabel('研究笔记正文')).toHaveValue('Window A: independently written finding.');
  await expect(page.getByText(/笔记已有新版本/)).toBeVisible();
  await page.screenshot({ path: screenshotPath('multiwindow-note-conflict'), fullPage: true });
  expect((await (await request.get(`/api/notes/${note.id}`)).json()).content).toBe('Window B: first committed finding.');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '完整 Markdown', exact: true }).click();
  const download = await downloadPromise;
  expect(await readFile((await download.path())!, 'utf8')).toContain('Window A: independently written finding.');
  await page.getByRole('button', { name: '另存为新笔记', exact: true }).click();
  await expect(page.getByLabel('研究笔记标题')).toHaveValue('Shared note（副本）');
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 1');
  const { notes } = await (await request.get('/api/notes')).json();
  expect(notes).toHaveLength(2);
  const copy = notes.find((value: { id: string }) => value.id !== note.id);
  expect((await (await request.get(`/api/notes/${copy.id}`)).json()).content).toBe('Window A: independently written finding.');
  const original = await (await request.get(`/api/notes/${note.id}`)).json();
  expect(original.content).toBe('Window B: first committed finding.');
  expect(original.versions).toHaveLength(2);
});

test('two chat windows rebase a genuine 409 conflict without losing either question or answer', async ({ page, context, request }) => {
  const paper = await readyPaper(request);
  const response = await request.post('/api/sessions', { data: { title: 'Shared research conversation', paper_ids: [paper.id], messages: [] } });
  expect(response.ok()).toBeTruthy();
  const session = await response.json();
  const second = await context.newPage();
  for (const window of [page, second]) {
    await window.goto('/');
    await nav(window, '文献问答');
    await expect(window.locator('.chat-panel-heading')).toContainText(session.title);
    await expect(window.locator('.chat-welcome')).toBeVisible();
  }
  let releaseFirst!: () => void;
  let signalHeld!: () => void;
  const held = new Promise<void>(resolve => { signalHeld = resolve; });
  const release = new Promise<void>(resolve => { releaseFirst = resolve; });
  const writes: number[] = [];
  let chatCalls = 0;
  let holdFirstWrite = true;
  context.on('request', value => { if (new URL(value.url()).pathname === '/api/chat') chatCalls++; });
  page.on('response', value => { if (value.url().endsWith(`/api/sessions/${session.id}`) && value.request().method() === 'PUT') writes.push(value.status()); });
  await page.route(`**/api/sessions/${session.id}`, async route => {
    if (route.request().method() === 'PUT' && holdFirstWrite) {
      holdFirstWrite = false;
      signalHeld();
      await release;
    }
    await route.continue();
  });
  await page.getByLabel('输入文献问题').fill(question);
  await page.getByRole('button', { name: '发送问题' }).click();
  await held;
  const otherQuestion = 'What was the batch size in the calibration run?';
  try {
    await second.getByLabel('输入文献问题').fill(otherQuestion);
    await second.getByRole('button', { name: '发送问题' }).click();
    await expect.poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).messages.at(-1)?.status).toBe('completed');
  } finally {
    releaseFirst();
  }
  await expect.poll(async () => {
    const saved = await (await request.get(`/api/sessions/${session.id}`)).json();
    return saved.messages.length === 4 && saved.messages.every((message: { status: string }) => message.status === 'completed');
  }).toBe(true);
  expect(writes).toContain(409);
  await expect.poll(() => writes.filter(value => value === 200).length).toBe(2);
  expect(chatCalls).toBe(2);
  for (const window of [page, second]) {
    await window.reload();
    await nav(window, '文献问答');
    await expect(window.locator('.message-user')).toHaveCount(2);
    await expect(window.locator('.message-assistant')).toHaveCount(2);
    await expect(window.locator('.conversation')).toContainText(question);
    await expect(window.locator('.conversation')).toContainText(otherQuestion);
    await expect(window.locator('.source-card').first()).toBeVisible();
  }
  await page.screenshot({ path: screenshotPath('multiwindow-chat-merged'), fullPage: true });
});

test('a dirty note prompts before closing and a committed note reloads without a prompt', async ({ page, request }) => {
  expect((await request.post('/api/notes', { data: { title: 'Close protection', content: 'Saved text', paper_ids: [], sources: [], kind: 'note' } })).ok()).toBeTruthy();
  await openNotes(page);
  await page.getByLabel('研究笔记正文').fill('Keep this local change until saved.');
  await cancelUnsavedClose(page);
  await expect(page.getByLabel('研究笔记正文')).toHaveValue('Keep this local change until saved.');
  await page.getByRole('button', { name: '保存版本', exact: true }).click();
  await expect(page.locator('.research-result-heading')).toContainText('已保存 · 版本 2');
  const unexpectedDialogs: string[] = [];
  page.on('dialog', async dialog => { unexpectedDialogs.push(dialog.type()); await dialog.dismiss(); });
  await page.reload();
  await nav(page, '研究写作');
  await expect(page.locator('.note-preview')).toContainText('Keep this local change until saved.');
  expect(unexpectedDialogs).toEqual([]);
});

test('an unsaved generated answer prompts before closing and survives a save retry', async ({ page, request }) => {
  const paper = await readyPaper(request);
  const created = await request.post('/api/sessions', { data: { title: 'Unsaved answer close protection', paper_ids: [paper.id], messages: [] } });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  await page.goto('/');
  await nav(page, '文献问答');
  await expect(page.locator('.chat-panel-heading')).toContainText('Unsaved answer close protection');
  let reject = true;
  let chatCalls = 0;
  page.on('request', value => { if (new URL(value.url()).pathname === '/api/chat') chatCalls++; });
  await page.route('**/api/sessions/*', async route => {
    if (route.request().method() === 'PUT' && reject && route.request().postDataJSON().messages.some((message: { role: string; status: string }) => message.role === 'assistant' && message.status === 'completed')) {
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Simulated unavailable storage' }) });
    }
    await route.continue();
  });
  await page.getByLabel('输入文献问题').fill(question);
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.getByRole('button', { name: '重试保存回答' })).toBeEnabled();
  const answer = await page.locator('.answer-content').innerText();
  await cancelUnsavedClose(page);
  await expect(page.locator('.answer-content')).toHaveText(answer, { useInnerText: true });
  reject = false;
  await page.getByRole('button', { name: '重试保存回答' }).click();
  // The button changes its label while saving; only the committed API state
  // and cleared unsaved notice are reliable signals that a reload is safe.
  await expect.poll(async () => (await (await request.get(`/api/sessions/${session.id}`)).json()).messages.at(-1)?.status).toBe('completed');
  await expect(page.getByText(/条回答尚未保存到数据库/)).toHaveCount(0);
  await page.reload();
  await nav(page, '文献问答');
  await expect(page.locator('.answer-content')).toHaveText(answer, { useInnerText: true });
  expect(chatCalls).toBe(1);
});
