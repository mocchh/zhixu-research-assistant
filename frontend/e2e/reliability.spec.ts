import { test, expect, cleanWorkspace, fixture, nav, screenshotPath } from './helpers';
import { readFile } from 'node:fs/promises';

test.beforeEach(async ({ request }) => cleanWorkspace(request));

test('switching model service clears its old key and disconnect survives reload', async ({ page, request, baseURL }) => {
  await page.goto('/');
  await page.locator('.settings-nav').click();
  await page.getByLabel('API 地址').fill(`${baseURL}/__e2e__/model`);
  await page.getByLabel('模型名称').fill('e2e-settings-only');
  await page.locator('#model-key').fill('fake-settings-only');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect.poll(async () => (await (await request.get('/api/settings')).json()).has_api_key).toBe(true);
  await expect(page.getByRole('button', { name: '测试连接', exact: true })).toBeEnabled();

  // A different service path must not silently inherit credentials either.
  await page.getByLabel('API 地址').fill(`${baseURL}/__e2e__/another-service`);
  await expect(page.getByText('服务地址已改变。', { exact: false })).toBeVisible();
  await page.screenshot({ path: screenshotPath('settings-key-isolation'), fullPage: true });
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect.poll(async () => (await (await request.get('/api/health')).json()).model_configured).toBe(false);
  await expect(page.getByRole('button', { name: '测试连接', exact: true })).toBeDisabled();
  await page.reload();
  await page.locator('.settings-nav').click();
  await expect(page.locator('#model-key')).toHaveValue('');
  await expect(page.getByRole('button', { name: '清除密钥', exact: true })).toBeDisabled();

  await page.locator('#model-key').fill('fake-new-service-only');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(page.getByRole('button', { name: '清除密钥', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '清除密钥', exact: true }).click();
  await expect.poll(async () => (await (await request.get('/api/settings')).json()).has_api_key).toBe(false);
  await page.reload();
  await page.locator('.settings-nav').click();
  await expect(page.getByRole('button', { name: '测试连接', exact: true })).toBeDisabled();
});

test('settings remain unavailable during initial and repeated loading, then save the exact entered service', async ({ page, request, baseURL }) => {
  const createGate = () => {
    let entered!: () => void;
    let release!: () => void;
    const ready = new Promise<void>(resolve => { entered = resolve; });
    const pending = new Promise<void>(resolve => { release = resolve; });
    return { entered, release, ready, pending };
  };
  const gates: ReturnType<typeof createGate>[] = [];
  let nextGate: ReturnType<typeof createGate> | null = null;
  await page.route('**/api/settings', async route => {
    const gate = route.request().method() === 'GET' ? nextGate : null;
    if (!gate) return route.continue();
    nextGate = null;
    // Read the real service first, then control only when its response reaches
    // the UI. The test never substitutes a settings payload.
    const response = await route.fetch();
    gate.entered();
    await gate.pending;
    await route.fulfill({ response });
  });
  try {
    await page.goto('/');
    for (const index of [1, 2]) {
      const current = await (await request.get('/api/settings')).json();
      const gate = createGate();
      gates.push(gate);
      nextGate = gate;
      if (index === 2) await nav(page, '文献库');
      await page.locator('.settings-nav').click();
      await gate.ready;
      await expect(page.getByText('正在加载配置…', { exact: true })).toBeVisible();
      await expect(page.locator('.settings-page form')).toHaveCount(0);
      await expect(page.getByLabel('API 地址')).toHaveCount(0);
      await expect(page.getByRole('button', { name: '保存配置', exact: true })).toHaveCount(0);
      gate.release();
      await expect(page.getByLabel('API 地址')).toHaveValue(current.base_url);
      await expect(page.getByLabel('模型名称')).toHaveValue(current.model);
      const targetUrl = `${baseURL}/__e2e__/settings-service-${index}`;
      const targetModel = `e2e-loaded-settings-${index}`;
      await page.getByLabel('API 地址').fill(targetUrl);
      await expect(page.getByLabel('API 地址')).toHaveValue(targetUrl);
      await page.getByLabel('模型名称').fill(targetModel);
      await expect(page.getByLabel('模型名称')).toHaveValue(targetModel);
      await page.locator('#model-key').fill(`fake-loaded-settings-${index}`);
      await page.getByRole('button', { name: '保存配置', exact: true }).click();
      await expect.poll(async () => {
        const saved = await (await request.get('/api/settings')).json();
        return { base_url: saved.base_url, model: saved.model, has_api_key: saved.has_api_key };
      }).toEqual({ base_url: targetUrl, model: targetModel, has_api_key: true });
      await expect(page.getByRole('button', { name: '保存配置', exact: true })).toBeEnabled();
    }
    await page.reload();
    await page.locator('.settings-nav').click();
    await expect(page.getByLabel('API 地址')).toHaveValue(`${baseURL}/__e2e__/settings-service-2`);
    await expect(page.getByLabel('模型名称')).toHaveValue('e2e-loaded-settings-2');
    await expect(page.locator('#model-key')).toHaveValue('');
    expect((await (await request.get('/api/settings')).json()).has_api_key).toBe(true);
  } finally {
    for (const gate of gates) gate.release();
    await page.unrouteAll({ behavior: 'wait' });
  }
});

test('mixed uploads show the successful paper and identify the failed file', async ({ page }) => {
  await page.goto('/');
  const response = page.waitForResponse(value => value.url().endsWith('/api/papers/upload'));
  await page.locator('input[type=file]').first().setInputFiles([
    { name: 'research.pdf', mimeType: 'application/pdf', buffer: await readFile(fixture()) },
    { name: 'broken-input.pdf', mimeType: 'application/pdf', buffer: Buffer.from('not a PDF') },
  ]);
  const result = await response;
  expect(result.status()).toBe(207);
  const body = await result.json();
  expect(body.papers).toHaveLength(1);
  expect(body.errors[0].filename).toBe('broken-input.pdf');
  await expect(page.locator('.paper-row')).toHaveCount(1);
  await expect(page.getByText(/broken-input\.pdf/).first()).toBeVisible();
  await expect(page.locator('.paper-row .status')).toHaveText('已就绪', { timeout: 20_000 });
  await page.screenshot({ path: screenshotPath('import-partial-result'), fullPage: true });
});
