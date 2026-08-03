import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import {
  cancelTjmVoiceCandidate,
  confirmTjmVoiceCandidate,
  openTjmAttemptItem,
  recordTjmAnswer,
  recordTjmVoiceCandidate,
  requestTjmHint,
  startTjmAttempt,
  startTjmReviewAttempt,
  submitTjmAttempt,
  tjmCommandInit,
} from '../lib/tjm-api'
import { hasGrade, normalizeAttemptForClient, type TjmAttempt } from '../lib/tjm-types'

function leakedAttempt(status: TjmAttempt['status']): TjmAttempt {
  return {
    id: 'att-1',
    exam_id: 'exam-1',
    mode: 'exam',
    status,
    exam_snapshot: {
      id: 'exam-1',
      title: 'Exam',
      description: '',
      duration_seconds: 60,
      question_count: 1,
      pass_score: 1,
      blueprint: {},
      revision: 1,
    },
    started_at: '2026-01-01T00:00:00Z',
    deadline_at: '2026-01-01T00:01:00Z',
    submitted_at: status === 'in_progress' ? null : '2026-01-01T00:00:30Z',
    correct_count: status === 'in_progress' ? null : 1,
    total_count: status === 'in_progress' ? null : 1,
    answered_count: 1,
    content_invalidated_count: 0,
    items: [
      {
        position: 0,
        question_version_id: 'qv-1',
        stable_id: 'q-1',
        stem: 'Question',
        choices: [
          { key: 'A', text: 'First' },
          { key: 'B', text: 'Second' },
        ],
        area: 'area',
        opened_at: null,
        answered_at: '2026-01-01T00:00:20Z',
        first_presented_at: '2026-01-01T00:00:00Z',
        first_answered_at: '2026-01-01T00:00:20Z',
        final_answered_at: '2026-01-01T00:00:20Z',
        confirmed_option_key: 'B',
        confidence: 80,
        elapsed_ms: 20_000,
        server_elapsed_ms: 20_000,
        client_active_elapsed_ms: 20_000,
        hint_count: 0,
        catalog_disposition: 'current',
        content_invalidated_at: null,
        grading_status: 'eligible',
        correct_option_key: 'B',
        explanation: 'Should stay hidden until submit.',
        is_correct: true,
      },
    ],
  }
}

test('in-progress exam normalization strips any leaked grade fields', () => {
  const normalized = normalizeAttemptForClient(leakedAttempt('in_progress'))
  assert.equal(hasGrade(normalized.items[0]), false)
  assert.equal('correct_option_key' in normalized.items[0], false)
  assert.equal('explanation' in normalized.items[0], false)
  assert.equal('is_correct' in normalized.items[0], false)
})

test('submitted exam normalization preserves server grading', () => {
  const normalized = normalizeAttemptForClient(leakedAttempt('submitted'))
  assert.equal(hasGrade(normalized.items[0]), true)
  if (hasGrade(normalized.items[0])) {
    assert.equal(normalized.items[0].correct_option_key, 'B')
    assert.equal(normalized.items[0].is_correct, true)
  }
})

test('TJM API client uses server grading and every required workflow route', () => {
  const source = readFileSync(path.resolve(process.cwd(), 'lib/tjm-api.ts'), 'utf8')
  assert.match(source, /const BASE = ["']\/api\/v1\/tjm["']/)
  for (const route of [
    '/exams',
    '/imports',
    '/review/questions',
    '/attempts',
    '/review/queue',
    '/review/attempts',
    '/history',
    '/analytics',
  ]) {
    assert.match(source, new RegExp(route.replaceAll('/', '\\/')))
  }
  assert.match(source, /\/items\/\$\{position\}\/open/)
  assert.doesNotMatch(source, /selected_option_key\s*===\s*correct_option_key/)
})

test('TJM command requests preserve the exact JSON body and idempotency key', () => {
  const input = {
    position: 2,
    selected_option_key: 'C',
    confidence: 70,
    elapsed_ms: 1_234,
    confirmed: true,
  }

  const init = tjmCommandInit('POST', input, 'command-key-123')

  assert.equal(init.method, 'POST')
  assert.deepEqual(init.headers, {
    'Content-Type': 'application/json',
    'Idempotency-Key': 'command-key-123',
  })
  assert.equal(init.body, JSON.stringify(input))
})

test('TJM command requests keep bodyless commands bodyless', () => {
  const init = tjmCommandInit('POST', undefined, 'submit-key-456')

  assert.deepEqual(init.headers, {
    'Content-Type': 'application/json',
    'Idempotency-Key': 'submit-key-456',
  })
  assert.equal('body' in init, false)
})

test('TJM learning commands send keys to their exact routes and opening is explicit', async () => {
  const originalFetch = globalThis.fetch
  const calls: Array<{ url: string; init?: RequestInit }> = []
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, init })
    const attemptResponse =
      url.endsWith('/attempts') ||
      url.endsWith('/submit') ||
      url.endsWith('/review/attempts')
        ? leakedAttempt(url.endsWith('/submit') ? 'submitted' : 'in_progress')
        : {}
    return new Response(JSON.stringify(attemptResponse), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  try {
    await startTjmAttempt('exam/a', 'practice', 'start-key')
    await openTjmAttemptItem('attempt/a', 3)
    await recordTjmAnswer(
      'attempt/a',
      {
        position: 3,
        selected_option_key: 'B',
        confidence: 80,
        elapsed_ms: 1_000,
        confirmed: true,
      },
      'answer-key'
    )
    await requestTjmHint('attempt/a', 3, 1_100, 'hint-key')
    await recordTjmVoiceCandidate('attempt/a', 3, '2番', 1_200, 'voice-key')
    await confirmTjmVoiceCandidate('attempt/a', 3, 9, 60, 1_300, 'confirm-key')
    await cancelTjmVoiceCandidate('attempt/a', 3, 9, 'cancel-key')
    await submitTjmAttempt('attempt/a', 'submit-key')
    await startTjmReviewAttempt('exam/a', 12, 'review-key')
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.deepEqual(
    calls.map(call => call.url),
    [
      '/api/v1/tjm/attempts',
      '/api/v1/tjm/attempts/attempt%2Fa/items/3/open',
      '/api/v1/tjm/attempts/attempt%2Fa/answers',
      '/api/v1/tjm/attempts/attempt%2Fa/items/3/hint',
      '/api/v1/tjm/attempts/attempt%2Fa/items/3/voice-candidate',
      '/api/v1/tjm/attempts/attempt%2Fa/items/3/voice-candidates/9/confirm',
      '/api/v1/tjm/attempts/attempt%2Fa/items/3/voice-candidates/9/cancel',
      '/api/v1/tjm/attempts/attempt%2Fa/submit',
      '/api/v1/tjm/review/attempts',
    ]
  )
  assert.equal(calls[1].init?.method, 'POST')
  assert.equal(new Headers(calls[1].init?.headers).has('Idempotency-Key'), false)
  assert.deepEqual(
    calls.filter((_, index) => index !== 1).map(call =>
      new Headers(call.init?.headers).get('Idempotency-Key')
    ),
    [
      'start-key',
      'answer-key',
      'hint-key',
      'voice-key',
      'confirm-key',
      'cancel-key',
      'submit-key',
      'review-key',
    ]
  )
})
