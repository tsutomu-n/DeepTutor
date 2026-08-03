import test from 'node:test'
import assert from 'node:assert/strict'

import {
  canAnswerTjmItem,
  canNavigateTjmAttempt,
  shouldRefreshExpiredTjmAttempt,
} from '../lib/tjm-attempt-state'

test('answers stay disabled until the server records item presentation', () => {
  assert.equal(
    canAnswerTjmItem({
      status: 'in_progress',
      mode: 'practice',
      confirmed: false,
      serverOpened: false,
      secondsLeft: null,
      gradingStatus: 'eligible',
    }),
    false
  )
})

test('practice and review answers lock after reveal while exam answers remain editable', () => {
  for (const mode of ['practice', 'review'] as const) {
    assert.equal(
      canAnswerTjmItem({
        status: 'in_progress',
        mode,
        confirmed: true,
        serverOpened: true,
        secondsLeft: null,
        gradingStatus: 'eligible',
      }),
      false
    )
  }
  assert.equal(
    canAnswerTjmItem({
      status: 'in_progress',
      mode: 'exam',
      confirmed: true,
      serverOpened: true,
      secondsLeft: 10,
      gradingStatus: 'eligible',
    }),
    true
  )
})

test('deadline zero disables input and requests authoritative final state', () => {
  assert.equal(
    canAnswerTjmItem({
      status: 'in_progress',
      mode: 'exam',
      confirmed: false,
      serverOpened: true,
      secondsLeft: 0,
      gradingStatus: 'eligible',
    }),
    false
  )
  assert.equal(shouldRefreshExpiredTjmAttempt('in_progress', 'exam', 0), true)
  assert.equal(shouldRefreshExpiredTjmAttempt('submitted', 'exam', 0), false)
  assert.equal(shouldRefreshExpiredTjmAttempt('in_progress', 'practice', 0), false)
})

test('content-invalidated questions cannot accept screen or voice answers', () => {
  assert.equal(
    canAnswerTjmItem({
      status: 'in_progress',
      mode: 'exam',
      confirmed: false,
      serverOpened: true,
      secondsLeft: 30,
      gradingStatus: 'content_invalidated',
    }),
    false
  )
})

test('navigation waits for server open acknowledgement and voice actions', () => {
  assert.equal(canNavigateTjmAttempt('in_progress', false, false), false)
  assert.equal(canNavigateTjmAttempt('in_progress', true, true), false)
  assert.equal(canNavigateTjmAttempt('in_progress', true, false), true)
  assert.equal(canNavigateTjmAttempt('expired', false, false), true)
})
