const {
  defineConfig
} = require('@playwright/test');
module.exports = defineConfig({
  testDir: '../tests/e2e/browser',
  testMatch: '*.spec.cjs',
  fullyParallel: false,
  workers: 1,
  timeout: 25000,
  expect: {
    timeout: 6000
  },
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list'], ['json', {
    outputFile: '../artifacts/workspace-replay/browser-results.json'
  }], ['html', {
    outputFolder: '../artifacts/workspace-replay/browser-report',
    open: 'never'
  }]],
  outputDir: '../artifacts/workspace-replay/browser-output',
  use: {
    baseURL: 'http://127.0.0.1:39240',
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure'
  },
  webServer: {
    command: 'node scripts/workspace-replay-server.cjs',
    url: 'http://127.0.0.1:39240',
    reuseExistingServer: false,
    timeout: 60000
  }
});
