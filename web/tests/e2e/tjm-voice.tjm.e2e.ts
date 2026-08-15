import { expect, test } from '@playwright/test'

import { ensureTjmFixtures } from './tjm-fixtures'

function silentWav(durationSeconds = 2, sampleRate = 16_000): Buffer {
  const sampleCount = durationSeconds * sampleRate
  const buffer = Buffer.alloc(44 + sampleCount * 2)
  buffer.write('RIFF', 0, 'ascii')
  buffer.writeUInt32LE(36 + sampleCount * 2, 4)
  buffer.write('WAVE', 8, 'ascii')
  buffer.write('fmt ', 12, 'ascii')
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(1, 20)
  buffer.writeUInt16LE(1, 22)
  buffer.writeUInt32LE(sampleRate, 24)
  buffer.writeUInt32LE(sampleRate * 2, 28)
  buffer.writeUInt16LE(2, 32)
  buffer.writeUInt16LE(16, 34)
  buffer.write('data', 36, 'ascii')
  buffer.writeUInt32LE(sampleCount * 2, 40)
  return buffer
}

test.beforeAll(async ({ request }) => {
  await ensureTjmFixtures(request)
})

test('実VADで音声候補を作り、取消後の再認識を確認してからだけ回答を保存する', async ({ page }) => {
  test.setTimeout(75_000)
  await page.addInitScript(() => {
    const mediaDevices = navigator.mediaDevices
    const originalGetUserMedia = mediaDevices.getUserMedia.bind(mediaDevices)
    const streams: MediaStream[] = []
    const streamsByRequest: Array<{ request: number; stream: MediaStream }> = []
    let getUserMediaRequests = 0
    let releaseFirstGetUserMedia!: () => void
    const firstGetUserMediaGate = new Promise<void>(resolve => {
      releaseFirstGetUserMedia = resolve
    })
    Object.defineProperty(window, '__tjmE2eMediaStreams', {
      configurable: false,
      value: streams,
    })
    Object.defineProperty(window, '__tjmE2eGetUserMediaRequests', {
      configurable: false,
      get: () => getUserMediaRequests,
    })
    Object.defineProperty(window, '__tjmE2eMediaStreamsByRequest', {
      configurable: false,
      value: streamsByRequest,
    })
    Object.defineProperty(window, '__tjmE2eReleaseFirstGetUserMedia', {
      configurable: false,
      value: releaseFirstGetUserMedia,
    })
    Object.defineProperty(mediaDevices, 'getUserMedia', {
      configurable: true,
      value: async (constraints: MediaStreamConstraints) => {
        getUserMediaRequests += 1
        const request = getUserMediaRequests
        if (request === 1) await firstGetUserMediaGate
        const stream = await originalGetUserMedia(constraints)
        streams.push(stream)
        streamsByRequest.push({ request, stream })
        return stream
      },
    })
  })
  let transcriptionRequests = 0
  let successfulTranscriptions = 0
  let holdNextTranscription = true
  let releaseTranscription!: () => void
  const transcriptionGate = new Promise<void>(resolve => {
    releaseTranscription = resolve
  })
  await page.route('**/api/v1/voice/stt', async route => {
    transcriptionRequests += 1
    if (holdNextTranscription) {
      holdNextTranscription = false
      await transcriptionGate
      await route
        .fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ text: '2番' }),
        })
        .catch(() => undefined)
      return
    }
    successfulTranscriptions += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ text: '2番' }),
    })
  })
  let holdNextSynthesis = true
  let releaseSynthesis!: () => void
  const synthesisGate = new Promise<void>(resolve => {
    releaseSynthesis = resolve
  })
  await page.route('**/api/v1/voice/tts', async route => {
    if (holdNextSynthesis) {
      holdNextSynthesis = false
      await synthesisGate
      await route
        .fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() })
        .catch(() => undefined)
      return
    }
    await route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() })
  })

  await page.goto('/tjm')
  const exam = page.locator('article').filter({ hasText: '汎用択一ミニ試験' }).first()
  await exam.getByRole('button', { name: '通常演習', exact: true }).click()
  await expect(page.getByRole('radio')).toHaveCount(4)
  await expect(page.getByRole('radio').first()).toBeEnabled()

  await page.getByRole('button', { name: '音声で回答', exact: true }).click()
  await expect(page.getByRole('button', { name: 'マイク開始を中止', exact: true })).toBeVisible()
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as typeof window & {
              __tjmE2eGetUserMediaRequests: number
            }
          ).__tjmE2eGetUserMediaRequests
      )
    )
    .toBe(1)
  await page.getByRole('button', { name: 'マイク開始を中止', exact: true }).click()
  await expect(page.getByRole('button', { name: '音声で回答', exact: true })).toBeVisible()
  await expect(page.getByRole('radio').first()).toBeEnabled()
  await page.getByRole('button', { name: '音声で回答', exact: true }).click()
  await expect(
    page.getByRole('button', { name: /マイクを停止|音声を検出しています/ })
  ).toBeVisible({ timeout: 10_000 })
  await expect
    .poll(() =>
      page.evaluate(() => {
        const records = (
          window as typeof window & {
            __tjmE2eMediaStreamsByRequest: Array<{
              request: number
              stream: MediaStream
            }>
          }
        ).__tjmE2eMediaStreamsByRequest
        return records
          .filter(record => record.request === 2)
          .some(record => record.stream.getTracks().some(track => track.readyState === 'live'))
      })
    )
    .toBe(true)
  await page.evaluate(() =>
    (
      window as typeof window & {
        __tjmE2eReleaseFirstGetUserMedia: () => void
      }
    ).__tjmE2eReleaseFirstGetUserMedia()
  )
  await expect
    .poll(() =>
      page.evaluate(() => {
        const records = (
          window as typeof window & {
            __tjmE2eMediaStreamsByRequest: Array<{
              request: number
              stream: MediaStream
            }>
          }
        ).__tjmE2eMediaStreamsByRequest
        const oldStreamEnded = records
          .filter(record => record.request === 1)
          .some(record => record.stream.getTracks().every(track => track.readyState === 'ended'))
        const newStreamLive = records
          .filter(record => record.request === 2)
          .some(record => record.stream.getTracks().some(track => track.readyState === 'live'))
        return (
          oldStreamEnded && newStreamLive
        )
      })
    )
    .toBe(true)
  await page.getByRole('button', { name: '問題を読み上げる', exact: true }).click()
  await expect(page.getByRole('button', { name: '読み上げを停止', exact: true })).toBeVisible()
  await expect
    .poll(() =>
      page.evaluate(() => {
        const streams = (
          window as typeof window & {
            __tjmE2eMediaStreams: MediaStream[]
          }
        ).__tjmE2eMediaStreams
        return (
          streams.length > 0 &&
          streams.every(stream => stream.getTracks().every(track => track.readyState === 'ended'))
        )
      })
    )
    .toBe(true)
  await page.getByRole('button', { name: '読み上げを停止', exact: true }).click()
  await expect(page.getByRole('button', { name: '問題を読み上げる', exact: true })).toBeVisible({
    timeout: 5_000,
  })
  await expect(page.getByRole('radio').first()).toBeEnabled()
  releaseSynthesis()
  await expect(page.getByRole('button', { name: /マイクを停止|音声を検出しています/ })).toHaveCount(0)

  await page.getByRole('button', { name: '音声で回答', exact: true }).click()
  await expect(page.getByRole('button', { name: '音声認識を中止', exact: true })).toBeVisible({
    timeout: 20_000,
  })
  await page.getByRole('button', { name: '音声認識を中止', exact: true }).click()
  await expect(page.getByRole('button', { name: '音声で回答', exact: true })).toBeVisible()
  await expect(page.getByRole('radio').first()).toBeEnabled()
  releaseTranscription()
  await expect(page.getByRole('alertdialog', { name: '音声回答を確認' })).toHaveCount(0)

  await page.getByRole('button', { name: '音声で回答', exact: true }).click()
  const firstCandidate = page.getByRole('alertdialog', { name: '音声回答を確認' })
  await expect(firstCandidate).toContainText(
    '「2番」と認識しました。選択肢2で確定しますか？確定するまで回答は保存されません。',
    { timeout: 20_000 }
  )
  await expect(page.getByText('0 / 3 問回答済み', { exact: true })).toBeVisible()
  await expect(page.getByText('確定済み', { exact: true })).toHaveCount(0)
  await firstCandidate.getByRole('button', { name: 'キャンセル', exact: true }).click()
  await expect(firstCandidate).toHaveCount(0)
  await expect(page.getByText('0 / 3 問回答済み', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '音声で回答', exact: true }).click()
  const secondCandidate = page.getByRole('alertdialog', { name: '音声回答を確認' })
  await expect(secondCandidate).toContainText('「2番」と認識しました。選択肢2で確定しますか？', {
    timeout: 20_000,
  })
  await secondCandidate.getByRole('button', { name: '回答を確定', exact: true }).click()
  await expect(secondCandidate).toHaveCount(0)
  await expect(page.getByText('確定済み', { exact: true })).toBeVisible()
  await expect(page.getByText('1 / 3 問回答済み', { exact: true })).toBeVisible()
  expect(transcriptionRequests).toBe(3)
  expect(successfulTranscriptions).toBe(2)
})
