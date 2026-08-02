import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'

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
        confirmed_option_key: 'B',
        confidence: 80,
        elapsed_ms: 20_000,
        hint_count: 0,
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
  assert.doesNotMatch(source, /selected_option_key\s*===\s*correct_option_key/)
})
