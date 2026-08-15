import test from 'node:test'
import assert from 'node:assert/strict'

import { TjmTtsLifecycleController } from '../lib/tjm-audio-lifecycle'

interface FakeAudioResource {
  name: string
}

test('a newer TTS request disposes the active audio and invalidates stale completion', () => {
  const disposed: string[] = []
  const controller = new TjmTtsLifecycleController<FakeAudioResource>(resource => {
    disposed.push(resource.name)
  })

  const firstToken = controller.begin()
  const first = { name: 'first' }
  assert.equal(controller.attach(firstToken, first), true)

  const secondToken = controller.begin()
  const second = { name: 'second' }
  assert.deepEqual(disposed, ['first'])
  assert.equal(controller.attach(secondToken, second), true)

  assert.equal(controller.finish(firstToken, first), false)
  assert.deepEqual(disposed, ['first'])
  assert.equal(controller.isCurrent(secondToken), true)

  assert.equal(controller.finish(secondToken, second), true)
  assert.deepEqual(disposed, ['first', 'second'])
  assert.equal(controller.isCurrent(secondToken), false)
})

test('a resource arriving after cancellation is disposed without becoming current', () => {
  const disposed: string[] = []
  const controller = new TjmTtsLifecycleController<FakeAudioResource>(resource => {
    disposed.push(resource.name)
  })

  const token = controller.begin()
  controller.stop()

  assert.equal(controller.attach(token, { name: 'late' }), false)
  assert.deepEqual(disposed, ['late'])
})

test('a stale failure cannot clear a newer TTS request', () => {
  const disposed: string[] = []
  const controller = new TjmTtsLifecycleController<FakeAudioResource>(resource => {
    disposed.push(resource.name)
  })

  const firstToken = controller.begin()
  const secondToken = controller.begin()
  const second = { name: 'second' }
  assert.equal(controller.attach(secondToken, second), true)

  assert.equal(controller.fail(firstToken), false)
  assert.equal(controller.isCurrent(secondToken), true)
  assert.deepEqual(disposed, [])

  assert.equal(controller.fail(secondToken), true)
  assert.deepEqual(disposed, ['second'])
})
