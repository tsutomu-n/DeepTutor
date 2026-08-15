import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { writeSyntheticSpeechLikeWav } from './tjm-e2e-audio.mjs'

const scriptRoot = path.dirname(fileURLToPath(import.meta.url))
const webRoot = path.resolve(scriptRoot, '..')
const playwrightCli = path.join(webRoot, 'node_modules', '@playwright', 'test', 'cli.js')
const runtimeHome = await mkdtemp(path.join(os.tmpdir(), 'deeptutor-tjm-e2e-'))
const fakeAudioPath =
  process.env.TJM_E2E_FAKE_AUDIO_PATH || path.join(runtimeHome, 'synthetic-microphone.wav')
if (!process.env.TJM_E2E_FAKE_AUDIO_PATH) {
  await writeSyntheticSpeechLikeWav(fakeAudioPath)
}

async function reserveLocalPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        server.close(() => reject(new Error('Could not reserve a local port')))
        return
      }
      const port = address.port
      server.close(error => (error ? reject(error) : resolve(port)))
    })
  })
}

const [apiPort, webPort] = await Promise.all([reserveLocalPort(), reserveLocalPort()])
const env = {
  ...process.env,
  UV_CACHE_DIR: path.join(os.tmpdir(), 'deeptutor-uv-cache'),
  TJM_E2E_HOME: runtimeHome,
  TJM_E2E_API_URL: `http://127.0.0.1:${apiPort}`,
  WEB_BASE_URL: `http://127.0.0.1:${webPort}`,
  TJM_E2E_FAKE_AUDIO_PATH: fakeAudioPath,
}

const child = spawn(
  process.execPath,
  [playwrightCli, 'test', '--config', 'playwright.tjm.config.ts', ...process.argv.slice(2)],
  { cwd: webRoot, env, stdio: 'inherit' }
)

let interrupted = false
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    interrupted = true
    child.kill(signal)
  })
}

const exitCode = await new Promise((resolve, reject) => {
  child.on('error', reject)
  child.on('exit', (code, signal) => {
    if (signal && !interrupted) {
      reject(new Error(`Playwright exited after ${signal}`))
      return
    }
    resolve(code ?? 1)
  })
})

if (exitCode === 0) {
  await rm(runtimeHome, { recursive: true, force: true })
} else {
  console.error(`TJM E2E runtime data preserved for diagnosis: ${runtimeHome}`)
}

process.exitCode = exitCode
