import test from 'node:test'
import assert from 'node:assert/strict'

import {
  getTjmExamPreferences,
  updateTjmExamPreference,
  updateTjmOfficialPassingScore,
} from '../lib/tjm-api'
import {
  getTjmNotEvaluatedReasonKey,
  getTjmResultDisplay,
  normalizeTjmAttemptResult,
  safeTjmSourceUrl,
  type TjmAttemptResult,
  type TjmOfficialPassingScoreSource,
} from '../lib/tjm-types'
import { tjmText } from '../i18n/tjm'

const source: TjmOfficialPassingScoreSource = {
  title: 'Published scoring notice',
  publisher: 'Exam authority',
  url: 'https://example.test/scoring',
  published_at: '2026-01-15',
}

function eligibleResult(): TjmAttemptResult {
  return {
    score: 0,
    maximum_score: 1,
    validity: 'eligible',
    official: {
      status: 'passed',
      threshold: 0,
      source,
      not_evaluated_reason: null,
    },
    practice_target: {
      status: 'achieved',
      threshold: 0,
      not_evaluated_reason: null,
    },
  }
}

test('result display keeps a zero threshold and separates official from personal outcomes', () => {
  const display = getTjmResultDisplay(eligibleResult())

  assert.deepEqual(display.official, {
    labelKey: 'result.status.officialPassed',
    threshold: 0,
    evaluated: true,
    positive: true,
    reasonKey: null,
  })
  assert.deepEqual(display.practiceTarget, {
    labelKey: 'result.status.practiceAchieved',
    threshold: 0,
    evaluated: true,
    positive: true,
    reasonKey: null,
  })
  assert.equal(tjmText(display.official.labelKey), '合格基準以上')
  assert.equal(tjmText(display.practiceTarget.labelKey), '目標達成')
})

test('content invalidation suppresses passed and achieved badges even if nested data regresses', () => {
  const result = { ...eligibleResult(), validity: 'content_invalidated' as const }
  const display = getTjmResultDisplay(result)

  assert.equal(display.official.labelKey, 'result.status.notEvaluated')
  assert.equal(display.official.evaluated, false)
  assert.equal(display.official.positive, false)
  assert.equal(display.official.reasonKey, 'result.reason.contentInvalidated')
  assert.equal(display.practiceTarget.labelKey, 'result.status.notEvaluated')
  assert.equal(display.practiceTarget.evaluated, false)
  assert.equal(display.practiceTarget.positive, false)
})

test('server not-evaluated reasons remain visible for both result dimensions', () => {
  const display = getTjmResultDisplay({
    ...eligibleResult(),
    official: {
      status: 'not_evaluated',
      threshold: null,
      source: null,
      not_evaluated_reason: 'official_score_unavailable',
    },
    practice_target: {
      status: 'not_evaluated',
      threshold: null,
      not_evaluated_reason: 'practice_target_unset',
    },
  })

  assert.equal(display.official.reasonKey, 'result.reason.officialScoreUnavailable')
  assert.equal(display.practiceTarget.reasonKey, 'result.reason.practiceTargetUnset')
  assert.equal(
    tjmText(display.official.reasonKey!),
    'この試験には、出典が確認された公式の合格基準点が登録されていません。'
  )
})

test('every not-evaluated reason has a stable copy key and unknown reasons fail closed', () => {
  assert.deepEqual(
    [
      'official_score_unavailable',
      'practice_target_unset',
      'mode_not_eligible',
      'content_invalidated',
      'incomplete_score_scope',
      'legacy_score_ambiguous',
    ].map(getTjmNotEvaluatedReasonKey),
    [
      'result.reason.officialScoreUnavailable',
      'result.reason.practiceTargetUnset',
      'result.reason.modeNotEligible',
      'result.reason.contentInvalidated',
      'result.reason.incompleteScoreScope',
      'result.reason.legacyScoreAmbiguous',
    ]
  )
  assert.equal(getTjmNotEvaluatedReasonKey('future_reason'), 'result.reason.unknown')
  assert.equal(getTjmNotEvaluatedReasonKey(null), 'result.reason.unknown')
})

test('an unknown not-evaluated reason cannot become a positive result badge', () => {
  const result = eligibleResult()
  result.official = {
    status: 'not_evaluated',
    threshold: null,
    source: null,
    not_evaluated_reason: 'future_reason' as never,
  }

  const display = getTjmResultDisplay(result)
  assert.equal(display.official.positive, false)
  assert.equal(display.practiceTarget.positive, false)
  assert.equal(display.official.reasonKey, 'result.reason.noProjection')
})

test('unknown or contradictory runtime result projections fail closed', () => {
  assert.equal(
    normalizeTjmAttemptResult({ ...eligibleResult(), validity: 'future_validity' }),
    null
  )
  assert.equal(
    normalizeTjmAttemptResult({
      ...eligibleResult(),
      official: { ...eligibleResult().official, threshold: 1 },
    }),
    null
  )
  assert.equal(
    normalizeTjmAttemptResult({
      ...eligibleResult(),
      official: {
        ...eligibleResult().official,
        source: { ...source, url: 'https://example.test/\tbad' },
      },
    }),
    null
  )
})

test('scoring sources only become links for absolute HTTP or HTTPS URLs', () => {
  assert.equal(safeTjmSourceUrl('https://example.test/notice'), 'https://example.test/notice')
  assert.equal(safeTjmSourceUrl('http://example.test/notice'), 'http://example.test/notice')
  assert.equal(safeTjmSourceUrl('javascript:alert(1)'), null)
  assert.equal(safeTjmSourceUrl('https://user:secret@example.test/notice'), null)
  assert.equal(safeTjmSourceUrl('https://example.test/\tbad'), null)
  assert.equal(safeTjmSourceUrl('/relative/notice'), null)
  assert.equal(safeTjmSourceUrl('not a URL'), null)
  assert.equal(safeTjmSourceUrl(null), null)
})

test('score settings API keeps official and personal thresholds on distinct routes', async () => {
  const originalFetch = globalThis.fetch
  const calls: Array<{ url: string; init?: RequestInit }> = []
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, init })
    if (url.endsWith('/exam-preferences')) {
      return Response.json({
        preferences: [
          {
            exam_id: 'exam-a',
            practice_target_score: 0,
            origin: 'legacy_pass_score',
            updated_at: null,
          },
        ],
        total: 1,
      })
    }
    if (url.endsWith('/exam-preferences/exam-a')) {
      return Response.json({
        exam_id: 'exam-a',
        practice_target_score: null,
        origin: 'user',
        updated_at: '2026-08-03T00:00:00Z',
      })
    }
    return Response.json({
      id: 'exam-a',
      title: 'Exam',
      description: '',
      duration_seconds: 60,
      question_count: 1,
      official_passing_score: 0,
      official_passing_score_source: source,
      blueprint: {},
      status: 'draft',
      revision: 2,
    })
  }

  try {
    const listed = await getTjmExamPreferences()
    assert.equal(listed.preferences[0]?.practice_target_score, 0)
    assert.equal(listed.preferences[0]?.origin, 'legacy_pass_score')
    await updateTjmExamPreference('exam-a', null)
    await updateTjmOfficialPassingScore('exam-a', {
      official_passing_score: 0,
      official_passing_score_source: source,
    })
  } finally {
    globalThis.fetch = originalFetch
  }

  assert.deepEqual(
    calls.map(call => call.url),
    [
      '/api/v1/tjm/exam-preferences',
      '/api/v1/tjm/exam-preferences/exam-a',
      '/api/v1/tjm/exams/exam-a/official-passing-score',
    ]
  )
  assert.equal(calls[0]?.init?.credentials, 'include')
  assert.equal(calls[0]?.init?.method, undefined)
  assert.equal(calls[0]?.init?.body, undefined)
  assert.equal(calls[1]?.init?.method, 'PUT')
  assert.equal(calls[1]?.init?.body, JSON.stringify({ practice_target_score: null }))
  assert.equal(calls[2]?.init?.method, 'PUT')
  assert.equal(
    calls[2]?.init?.body,
    JSON.stringify({
      official_passing_score: 0,
      official_passing_score_source: source,
    })
  )
  for (const call of calls.slice(1)) {
    assert.equal(new Headers(call.init?.headers).has('X-CSRF-Token'), false)
  }
})
