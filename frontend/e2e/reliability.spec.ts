import { test, expect, cleanWorkspace, fixture, screenshotPath } from './helpers';
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
