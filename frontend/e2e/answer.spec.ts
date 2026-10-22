import type { APIRequestContext, Page } from '@playwright/test';
import { test, expect, cleanWorkspace, readyPaper, nav, selectPaper, question, assertHighlightVisible, screenshotPath } from './helpers';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

async function configureLocalModel(page: Page, request: APIRequestContext, baseURL: string, model: string) {
  const current = await (await request.get('/api/settings')).json();
  const targetUrl = `${baseURL}/__e2e__/model`;
  await page.goto('/');
  await page.locator('.settings-nav').click();
  await expect(page.getByLabel('API 地址')).toHaveValue(current.base_url);
  await page.getByLabel('API 地址').fill(targetUrl);
  await expect(page.getByLabel('API 地址')).toHaveValue(targetUrl);
  await page.getByLabel('模型名称').fill(model);
  await expect(page.getByLabel('模型名称')).toHaveValue(model);
  await page.locator('#model-key').fill('e2e-contract-only');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect.poll(async () => {
    const saved = await (await request.get('/api/settings')).json();
    return { base_url: saved.base_url, model: saved.model, has_api_key: saved.has_api_key };
  }).toEqual({ base_url: targetUrl, model, has_api_key: true });
  await nav(page, '文献库');
  await selectPaper(page);
  await nav(page, '文献问答');
}

async function ask(page: Page) {
  const response = page.waitForResponse(value => value.url().endsWith('/api/chat') && value.request().method() === 'POST');
  await page.getByRole('textbox', { name: '输入文献问题' }).fill(question);
  await page.getByRole('button', { name: '发送问题' }).click();
  const result = await response;
  expect(result.ok()).toBeTruthy();
  return result.json();
}

async function savedSession(request: APIRequestContext) {
  await expect.poll(async () => {
    const { sessions } = await (await request.get('/api/sessions')).json();
    return sessions.length ? (await (await request.get(`/api/sessions/${sessions[0].id}`)).json()).messages.at(-1)?.status : '';
  }).toBe('completed');
  const { sessions } = await (await request.get('/api/sessions')).json();
  return (await request.get(`/api/sessions/${sessions[0].id}`)).json();
}

test('structured claims keep individual citations that open the real PDF source', async ({ page, request, baseURL }) => {
  const paper = await readyPaper(request);
  await configureLocalModel(page, request, baseURL!, 'e2e-contract-model');
  const result = await ask(page);
  expect(result.mode).toBe('model');
  expect(result.answer_audit.status).toBe('checked');
  expect(result.answer_audit.semantic_entailment_verified).toBe(false);
  const structured = JSON.parse(result.answer_audit.raw_response);
  expect(structured.claims).toHaveLength(2);
  for (const claim of structured.claims) {
    expect(claim.source_indices).toHaveLength(1);
    const source = result.sources[claim.source_indices[0] - 1];
    expect(source.paper_id).toBe(paper.id);
    expect(source.page).toBe(2);
    expect(source.text).toContain('8 NVIDIA P100 GPUs');
  }
  const assistant = page.locator('.message-assistant');
  await expect(assistant.locator('.mode-tag')).toContainText('基于文献生成');
  await expect(assistant.locator('.markdown')).toContainText('8 NVIDIA P100 GPUs');
  await expect(assistant.locator('.markdown')).toContainText('12 hours');
  await expect(assistant.locator('.citation-link')).toHaveCount(2);
  await expect(assistant.locator('.answer-audit')).toHaveCount(0);
  const saved = await savedSession(request);
  expect(saved.messages.at(-1).answer_audit).toEqual(result.answer_audit);
  await assistant.locator('.citation-link').first().click();
  await expect(page.getByRole('spinbutton', { name: '当前页码' })).toHaveValue('2');
  await assertHighlightVisible(page);
  await page.screenshot({ path: screenshotPath('answer-citation-desktop') });
});

test('incorrect claim falls back to excerpts while its collapsed audit survives a mobile reload', async ({ page, request, baseURL }) => {
  await readyPaper(request);
  await configureLocalModel(page, request, baseURL!, 'e2e-incorrect-answer');
  await page.setViewportSize({ width: 390, height: 844 });
  let chatRequests = 0;
  let completedSaveStarted = false;
  let gated = false;
  const gateKey = `audit-reading-${test.info().project.name}`;
  const gatePath = `/__e2e__/session-refresh-gates/${gateKey}`;
  expect((await request.post(gatePath)).ok()).toBeTruthy();
  try {
    page.on('request', value => {
      if (value.url().endsWith('/api/chat') && value.method() === 'POST') chatRequests += 1;
      if (value.url().includes('/api/sessions/') && value.method() === 'PUT') {
        completedSaveStarted ||= value.postDataJSON().messages.some(
          (message: { role: string; status: string }) => message.role === 'assistant' && message.status === 'completed');
      }
    });
    await page.route('**/api/sessions', async route => {
      if (route.request().method() === 'GET' && completedSaveStarted && !gated) {
        gated = true;
        await route.continue({ headers: { ...route.request().headers(), 'x-e2e-session-gate': gateKey } });
      } else await route.continue();
    });
    const result = await ask(page);
    expect(result.mode).toBe('extractive');
    expect(result.answer_audit.status).toBe('fallback');
    expect(result.answer_audit.semantic_entailment_verified).toBe(false);
    expect(result.answer_audit.raw_response).toContain('999 NVIDIA P100 GPUs');
    expect(result.answer_audit.draft).toContain('999 NVIDIA P100 GPUs');
    expect(result.answer_audit.issues.length).toBeGreaterThan(0);
    const assistant = page.locator('.message-assistant');
    await expect(assistant.locator('.mode-tag')).toContainText('原文检索摘录');
    await expect(assistant.locator('.markdown')).toContainText('8 NVIDIA P100 GPUs');
    await expect(assistant.locator('.markdown')).not.toContainText('999');
    await expect(assistant.locator('.citation-link')).toHaveCount(0);
    // The answer is already durable. Only its background list refresh remains
    // pending, so releasing it must not move the text the user is inspecting.
    await expect.poll(async () => (await (await request.get(gatePath)).json()).entered,
      { message: 'The final session list refresh is waiting after the answer save' }).toBe(true);
    await expect(page.getByRole('button', { name: '停止', exact: true })).toBeVisible();
    const saved = await savedSession(request);
    expect(saved.messages.at(-1).answer_audit).toEqual(result.answer_audit);
    const audit = assistant.locator('.answer-audit');
    await expect(audit).toBeVisible();
    await expect(audit.locator('pre')).toBeHidden();
    await audit.locator('summary').click();
    await expect(audit.locator('pre')).toBeVisible();
    await expect(audit.locator('pre')).toContainText('999 NVIDIA P100 GPUs');
    const readingPosition = await page.locator('.conversation').evaluate(element => ({ conversation: element.scrollTop, window: window.scrollY }));
    expect((await request.delete(gatePath)).ok()).toBeTruthy();
    await expect(page.getByRole('button', { name: '停止', exact: true })).toHaveCount(0);
    const settledPosition = await page.locator('.conversation').evaluate(element => ({ conversation: element.scrollTop, window: window.scrollY }));
    expect(Math.abs(settledPosition.conversation - readingPosition.conversation)).toBeLessThanOrEqual(1);
    expect(Math.abs(settledPosition.window - readingPosition.window)).toBeLessThanOrEqual(1);
    await expect(audit).toHaveAttribute('open', '');
    await expect(audit.locator('pre')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: screenshotPath('answer-fallback-mobile') });
    await page.reload();
    await nav(page, '文献问答');
    await expect(assistant.locator('.mode-tag')).toContainText('原文检索摘录');
    await expect(assistant.locator('.markdown')).not.toContainText('999');
    await expect(audit.locator('pre')).toBeHidden();
    await audit.locator('summary').click();
    await expect(audit.locator('pre')).toContainText('999 NVIDIA P100 GPUs');
    await expect(audit.locator('pre')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    const reloaded = await (await request.get(`/api/sessions/${saved.id}`)).json();
    expect(reloaded.messages).toEqual(saved.messages);
    expect(chatRequests).toBe(1);
  } finally {
    expect((await request.delete(gatePath)).ok()).toBeTruthy();
  }
});
