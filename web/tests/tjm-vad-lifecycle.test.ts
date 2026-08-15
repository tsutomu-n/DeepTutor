import test from 'node:test'
import assert from 'node:assert/strict'

import { TjmVadLifecycleController } from '../lib/tjm-vad-lifecycle'

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

class FakeVad {
  listening = false
  starts = 0
  pauses = 0
  destroys = 0

  constructor(private readonly startAction?: (vad: FakeVad) => Promise<void>) {}

  async start(): Promise<void> {
    this.starts += 1
    if (this.startAction) {
      await this.startAction(this)
      return
    }
    this.listening = true
  }

  async pause(): Promise<void> {
    this.pauses += 1
    this.listening = false
  }

  async destroy(): Promise<void> {
    this.destroys += 1
    this.listening = false
  }
}

test('stop invalidates delayed VAD initialization before the microphone starts', async () => {
  const controller = new TjmVadLifecycleController<FakeVad>()
  const factory = deferred<FakeVad>()
  const vad = new FakeVad()

  const starting = controller.start(() => factory.promise)
  const stopping = controller.stop()
  factory.resolve(vad)

  await stopping
  assert.equal(await starting, false)
  assert.equal(vad.starts, 0)
  assert.equal(vad.listening, false)
  assert.equal(vad.destroys, 1)
})

test('cancelStart returns immediately while late initialization is cleaned in background', async () => {
  let emergencyStops = 0
  const controller = new TjmVadLifecycleController<FakeVad>(() => {
    emergencyStops += 1
  })
  const factory = deferred<FakeVad>()
  const vad = new FakeVad()

  const starting = controller.start(() => factory.promise)
  controller.cancelStart()
  assert.equal(emergencyStops, 1)

  factory.resolve(vad)
  assert.equal(await starting, false)
  assert.equal(vad.starts, 0)
  assert.equal(vad.destroys, 1)
})

test('stop waits for an in-flight VAD start and closes a microphone opened later', async () => {
  const controller = new TjmVadLifecycleController<FakeVad>()
  const startGate = deferred<void>()
  const vad = new FakeVad(async current => {
    await startGate.promise
    current.listening = true
  })

  const starting = controller.start(async () => vad)
  await Promise.resolve()
  assert.equal(vad.starts, 1)

  const stopping = controller.stop()
  startGate.resolve()
  await stopping

  assert.equal(await starting, false)
  assert.equal(vad.pauses, 1)
  assert.equal(vad.listening, false)
})

test('failed VAD instances are destroyed and the next start creates a new one', async () => {
  const controller = new TjmVadLifecycleController<FakeVad>()
  const failedVad = new FakeVad(async () => {
    throw new Error('permission denied')
  })
  const replacementVad = new FakeVad()
  let factoryCalls = 0

  await assert.rejects(
    controller.start(async () => {
      factoryCalls += 1
      return failedVad
    }),
    /permission denied/
  )
  assert.equal(failedVad.destroys, 1)

  assert.equal(
    await controller.start(async () => {
      factoryCalls += 1
      return replacementVad
    }),
    true
  )
  assert.equal(factoryCalls, 2)
  assert.equal(replacementVad.listening, true)
})

test('failed VAD cleanup invokes the emergency microphone stop', async () => {
  let emergencyStops = 0
  const controller = new TjmVadLifecycleController<FakeVad>(() => {
    emergencyStops += 1
  })
  const vad = new FakeVad(async current => {
    current.listening = true
    throw new Error('worklet initialization failed')
  })
  vad.destroy = async () => {
    vad.destroys += 1
    throw new Error('partial VAD cannot destroy itself')
  }

  await assert.rejects(controller.start(async () => vad), /worklet initialization failed/)
  assert.equal(vad.destroys, 1)
  assert.equal(emergencyStops, 1)
})

test('discard invokes the emergency microphone stop even when destroy succeeds', async () => {
  let emergencyStops = 0
  const controller = new TjmVadLifecycleController<FakeVad>(() => {
    emergencyStops += 1
  })
  const vad = new FakeVad(async () => {
    throw new Error('worklet initialization failed after acquiring a stream')
  })

  await assert.rejects(controller.start(async () => vad), /worklet initialization failed/)
  assert.equal(vad.destroys, 1)
  assert.equal(emergencyStops, 1)
})

test('start succeeds only when the VAD reports that it is actually listening', async () => {
  const controller = new TjmVadLifecycleController<FakeVad>()
  const silentVad = new FakeVad(async () => {})

  await assert.rejects(controller.start(async () => silentVad), /did not start listening/)
  assert.equal(silentVad.destroys, 1)
})

test('destroy invalidates delayed initialization and destroys the late VAD', async () => {
  const controller = new TjmVadLifecycleController<FakeVad>()
  const factory = deferred<FakeVad>()
  const vad = new FakeVad()

  const starting = controller.start(() => factory.promise)
  const destroying = controller.destroy()
  factory.resolve(vad)

  await destroying
  assert.equal(await starting, false)
  assert.equal(vad.starts, 0)
  assert.equal(vad.destroys, 1)
})
