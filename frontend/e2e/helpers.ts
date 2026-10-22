import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

export const fixture = (name = 'research.pdf') => path.join(process.env.E2E_FIXTURE_DIR!, name);
export const question = 'How many NVIDIA P100 GPUs were used in the calibration run and for how many hours?';

export const test = base.extend<{ browserSafety: void }>({
  browserSafety: [async ({ browser, context, page, baseURL }, use, testInfo) => {
    testInfo.annotations.push({ type: 'browser-version', description: browser.version() });
    const external: string[] = [];
    const errors: string[] = [];
    // Include secondary windows: conflict tests must have the same network and
    // uncaught-error checks as the initial page.
    page.on('pageerror', error => errors.push(error.message));
    context.on('page', other => other.on('pageerror', error => errors.push(error.message)));
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.protocol === 'http:' || url.protocol === 'https:') {
        if (url.origin !== baseURL) {
          external.push(url.origin);
          await route.abort('blockedbyclient');
          return;
        }
      }
      await route.continue();
    });
    await use();
    expect(external, 'The regression suite must not contact external services').toEqual([]);
    expect(errors, 'No uncaught browser errors').toEqual([]);
  }, { auto: true }],
});
export { expect };

export const screenshotPath = (name: string) => path.join(
  process.env.E2E_REPORT_DIR || 'test-results/e2e', `${test.info().project.name}-${name}.png`,
);

export async function cleanWorkspace(request: APIRequestContext) {
  expect((await request.post('/__e2e__/reset-model')).ok()).toBeTruthy();
  for (const resource of ['sessions', 'notes']) {
    const result = await request.get(`/api/${resource}`);
    expect(result.ok()).toBeTruthy();
    for (const item of (await result.json())[resource]) {
      expect((await request.delete(`/api/${resource}/${item.id}`)).ok()).toBeTruthy();
    }
  }
  const { jobs } = await (await request.get('/api/jobs')).json();
  for (const job of jobs) {
    if (['queued', 'pending', 'running', 'processing', 'paused', 'pause_requested'].includes(job.status)) {
      await request.post(`/api/jobs/${job.id}/cancel`);
      await expect.poll(async () => (await (await request.get(`/api/jobs/${job.id}`)).json()).status).toMatch(/cancelled|done|error/);
    }
  }
  const { papers } = await (await request.get('/api/papers')).json();
  for (const paper of papers) expect((await request.delete(`/api/papers/${paper.id}`)).ok()).toBeTruthy();
}

export async function readyPaper(request: APIRequestContext, name = 'research.pdf') {
  const response = await request.post('/api/papers/upload', {
    multipart: { files: { name, mimeType: 'application/pdf', buffer: await readFile(fixture(name)) } },
  });
  expect(response.ok()).toBeTruthy();
  const paper = (await response.json()).papers[0];
  await waitPaper(request, paper.id, 'ready');
  return (await (await request.get(`/api/papers/${paper.id}`)).json()).paper;
}

export async function waitPaper(request: APIRequestContext, id: string, status: string) {
  await expect.poll(async () => (await (await request.get(`/api/papers/${id}`)).json()).paper.status,
    { timeout: 25_000, message: `Paper ${id} reaches ${status}` }).toBe(status);
}

export async function nav(page: Page, label: string) {
  const toggle = page.getByRole('button', { name: '切换导航', exact: true });
  if (await toggle.isVisible() && !await page.locator('.sidebar').evaluate(element => element.classList.contains('mobile-open'))) await toggle.click();
  await page.locator('.main-nav').getByRole('button', { name: label }).click();
}

export async function selectPaper(page: Page) {
  await page.locator('.paper-row input[type=checkbox]').first().check();
}

export async function goPage(page: Page, number: number) {
  const input = page.getByRole('spinbutton', { name: '当前页码' });
  await input.fill(String(number));
  await input.press('Enter');
  await expect(input).toHaveValue(String(number));
  await expect.poll(() => page.locator('.paper-image').evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
}

export async function assertHighlightVisible(page: Page) {
  await expect(page.locator('.evidence-highlight')).toBeVisible();
  await expect.poll(() => page.locator('.paper-scroll').evaluate(pane => {
    const marker = pane.querySelector('.evidence-highlight')!;
    const viewport = pane.getBoundingClientRect();
    const box = marker.getBoundingClientRect();
    return box.height > 0 && box.top >= viewport.top - 2 && box.bottom <= viewport.bottom + 2;
  }), { message: 'Evidence must be visible inside the PDF scroll pane' }).toBe(true);
}
