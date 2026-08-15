import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'

function requiredUrl(name: string): URL {
  const value = process.env[name]
  if (!value) throw new Error(`${name} must be set by scripts/run-tjm-e2e.mjs`)
  const url = new URL(value)
  if (url.protocol !== 'http:' || (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost')) {
    throw new Error(`${name} must use a local HTTP origin`)
  }
  return url
}

function portOf(url: URL): string {
  if (!url.port) throw new Error(`${url.href} must include an explicit port`)
  return url.port
}

const webRoot = process.cwd()
const repositoryRoot = path.resolve(webRoot, '..')
const baseUrl = requiredUrl('WEB_BASE_URL')
const apiUrl = requiredUrl('TJM_E2E_API_URL')
const runtimeHome = process.env.TJM_E2E_HOME
if (!runtimeHome || !path.isAbsolute(runtimeHome)) {
  throw new Error('TJM_E2E_HOME must be an absolute temporary directory')
}

const chromeExecutable = process.env.PLAYWRIGHT_CHROME_PATH
const fakeAudioPath = process.env.TJM_E2E_FAKE_AUDIO_PATH
const chromeArgs = [
  '--use-fake-ui-for-media-stream',
  '--use-fake-device-for-media-stream',
  ...(fakeAudioPath ? [`--use-file-for-fake-audio-capture=${fakeAudioPath}`] : []),
]

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.tjm.e2e.ts',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  outputDir: 'test-results/tjm',
  reporter: [['list'], ['html', { outputFolder: 'playwright-report/tjm', open: 'never' }]],
  use: {
    baseURL: baseUrl.origin,
    locale: 'ja-JP',
    timezoneId: 'Asia/Tokyo',
    permissions: ['microphone'],
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: chromeExecutable ? 'off' : 'retain-on-failure',
    launchOptions: {
      ...(chromeExecutable ? { executablePath: chromeExecutable } : {}),
      args: chromeArgs,
    },
  },
  webServer: [
    {
      command: `uv run uvicorn deeptutor.api.main:app --host 127.0.0.1 --port ${portOf(apiUrl)}`,
      cwd: repositoryRoot,
      env: {
        DEEPTUTOR_HOME: runtimeHome,
      },
      url: `${apiUrl.origin}/api/v1/auth/status`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --hostname 127.0.0.1 --port ${portOf(baseUrl)}`,
      cwd: webRoot,
      env: {
        DEEPTUTOR_API_BASE_URL: apiUrl.origin,
      },
      url: baseUrl.origin,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
  projects: [
    {
      name: 'tjm-desktop',
      testIgnore: '**/tjm-mobile.tjm.e2e.ts',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'tjm-mobile',
      testMatch: '**/tjm-mobile.tjm.e2e.ts',
      use: { ...devices['Pixel 7'] },
    },
  ],
})
