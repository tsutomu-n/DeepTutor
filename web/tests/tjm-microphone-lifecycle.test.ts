import test from 'node:test'
import assert from 'node:assert/strict'

import { TjmMicrophoneStreamRegistry } from '../lib/tjm-microphone-lifecycle'

class FakeTrack {
  stops = 0

  stop(): void {
    this.stops += 1
  }
}

class FakeStream {
  readonly track = new FakeTrack()

  getTracks(): FakeTrack[] {
    return [this.track]
  }
}

test('cleanup for an old VAD generation cannot stop the new microphone', () => {
  const registry = new TjmMicrophoneStreamRegistry<FakeStream>()
  const oldStream = new FakeStream()
  const newStream = new FakeStream()
  registry.add(1, oldStream)
  registry.add(2, newStream)

  registry.stopGeneration(1)

  assert.equal(oldStream.track.stops, 1)
  assert.equal(newStream.track.stops, 0)
  assert.equal(registry.has(1, oldStream), false)
  assert.equal(registry.has(2, newStream), true)
})

test('unmount cleanup stops every owned microphone generation', () => {
  const registry = new TjmMicrophoneStreamRegistry<FakeStream>()
  const oldStream = new FakeStream()
  const newStream = new FakeStream()
  registry.add(1, oldStream)
  registry.add(2, newStream)

  registry.stopAll()

  assert.equal(oldStream.track.stops, 1)
  assert.equal(newStream.track.stops, 1)
})
