import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import {
  TJM_LOCALE,
  hasTjmText,
  tjmCodeText,
  tjmText,
  type TjmTextKey,
} from '../i18n/tjm'

const REQUIRED_KEYS = [
  'workspace.title',
  'workspace.nav.learn',
  'exam.start.practice',
  'exam.start.timed',
  'attempt.progress',
  'attempt.answer.confirm',
  'voice.confirmCandidate',
  'result.dimension.official',
  'result.status.officialPassed',
  'result.status.practiceAchieved',
  'review.reason.incorrect',
  'analytics.confidence.low',
  'admin.review.action.publish',
  'aria.workspace',
] as const satisfies readonly TjmTextKey[]

test('TJM uses a complete fixed Japanese namespace for critical learner and admin flows', () => {
  assert.equal(TJM_LOCALE, 'ja')
  for (const key of REQUIRED_KEYS) assert.equal(hasTjmText(key), true, key)

  assert.equal(tjmText('workspace.title'), 'TJM 試験学習')
  assert.equal(
    tjmText('attempt.progress', { answered: 2, total: 7 }),
    '2 / 7 問回答済み'
  )
  assert.equal(
    tjmText('voice.confirmCandidate', { transcript: '3番', option: 'C' }),
    '「3番」と認識しました。選択肢Cで確定しますか？確定するまで回答は保存されません。'
  )
  assert.equal(tjmText('result.status.officialPassed'), '合格基準以上')
  assert.equal(tjmText('result.status.practiceAchieved'), '目標達成')
})

test('TJM code labels are explicit and unknown values fail closed in Japanese', () => {
  assert.equal(tjmCodeText('attempt.mode', 'practice'), '通常演習')
  assert.equal(tjmCodeText('review.reason', 'low_confidence'), '自信度が低い')
  assert.equal(tjmCodeText('analytics.confidence', 'high'), '高')
  assert.equal(tjmCodeText('attempt.mode', 'future_mode'), '不明')
})

test('Japanese TJM copy stays exam-generic and contains no Takken defaults', () => {
  const catalog = readFileSync(
    path.resolve(process.cwd(), 'locales/ja/tjm.json'),
    'utf8'
  )
  assert.doesNotMatch(catalog, /宅建|宅地建物取引士|権利関係|宅建業法|法令上の制限/)
  assert.doesNotMatch(catalog, /50問|120分/)
})
