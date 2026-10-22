import { defineConfig } from '@playwright/test';
import path from 'node:path';

const baseURL = process.env.E2E_BASE_URL;
if (!baseURL || !/^http:\/\/127\.0\.0\.1:\d+$/.test(baseURL) || [8000, 5173].includes(Number(new URL(baseURL).port)) || !process.env.E2E_FIXTURE_DIR) {
  throw new Error('Run npm run test:e2e to provision the isolated real service. Production ports are forbidden.');
}

const browsers = (process.env.E2E_BROWSERS || 'chromium').split(',').map(value => value.trim());
if (!browsers.length || new Set(browsers).size !== browsers.length || browsers.some(browser => !['chromium', 'firefox', 'webkit'].includes(browser))) {
  throw new Error('E2E_BROWSERS must contain distinct chromium, firefox, or webkit names separated by commas.');
}
const reportDir = process.env.E2E_REPORT_DIR || './test-results/e2e';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  outputDir: path.join(reportDir, 'artifacts'),
  reporter: [
    ['list'],
    ['junit', { outputFile: path.join(reportDir, 'junit.xml') }],
    ['json', { outputFile: path.join(reportDir, 'report.json') }],
    ['html', { outputFolder: path.join(reportDir, 'html'), open: 'never' }],
  ],
  projects: browsers.map(name => ({
    name,
    use: {
      browserName: name as 'chromium' | 'firefox' | 'webkit',
      // On macOS, -profile does not isolate Firefox's app-data directory.
      // Keep that directory in the disposable workspace as well, without
      // reading the user's Firefox data or changing operating-system privacy.
      ...(name === 'firefox' && process.platform === 'darwin' && process.env.E2E_FIREFOX_USER_HOME
        ? { launchOptions: { env: { ...process.env, CFFIXED_USER_HOME: process.env.E2E_FIREFOX_USER_HOME } } }
        : {}),
    },
  })),
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 960 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 10_000,
  },
});
