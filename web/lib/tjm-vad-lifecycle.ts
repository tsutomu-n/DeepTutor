export interface TjmVadInstance {
  listening: boolean
  start(): Promise<void>
  pause(): Promise<void>
  destroy(): Promise<void>
}

export type TjmVadFactory<T extends TjmVadInstance> = () => Promise<T>

export class TjmVadLifecycleController<T extends TjmVadInstance> {
  private vad: T | null = null
  private revision = 0
  private pendingStart: Promise<boolean> | null = null
  private disposed = false

  constructor(private readonly emergencyStop: () => void = () => undefined) {}

  start(factory: TjmVadFactory<T>): Promise<boolean> {
    if (this.disposed) return Promise.resolve(false)
    if (this.pendingStart) return this.pendingStart
    if (this.vad?.listening) return Promise.resolve(true)

    const revision = ++this.revision
    let pending!: Promise<boolean>
    pending = this.startCurrent(factory, revision).finally(() => {
      if (this.pendingStart === pending) this.pendingStart = null
    })
    this.pendingStart = pending
    return pending
  }

  cancelStart(): void {
    if (this.disposed) return
    this.revision += 1
    this.emergencyStop()
  }

  async stop(): Promise<void> {
    this.revision += 1
    const pending = this.pendingStart
    if (pending) await pending.catch(() => undefined)

    const vad = this.vad
    if (!vad?.listening) return
    try {
      await vad.pause()
    } catch (reason) {
      await this.discard(vad)
      throw reason
    }
  }

  async destroy(): Promise<void> {
    if (this.disposed) {
      const pending = this.pendingStart
      if (pending) await pending.catch(() => undefined)
      return
    }

    this.disposed = true
    this.revision += 1
    const pending = this.pendingStart
    if (pending) await pending.catch(() => undefined)

    const vad = this.vad
    this.vad = null
    if (vad) {
      try {
        await vad.destroy()
      } finally {
        this.emergencyStop()
      }
    }
  }

  private isCurrent(revision: number): boolean {
    return !this.disposed && revision === this.revision
  }

  private async startCurrent(factory: TjmVadFactory<T>, revision: number): Promise<boolean> {
    let vad = this.vad
    try {
      if (!vad) {
        vad = await factory()
        if (!this.isCurrent(revision)) {
          await this.discard(vad)
          return false
        }
        this.vad = vad
      }

      if (!this.isCurrent(revision)) return false
      await vad.start()

      if (!this.isCurrent(revision)) {
        if (vad.listening) {
          await vad.pause()
        } else {
          await this.discard(vad)
          vad = null
        }
        return false
      }

      if (!vad.listening) {
        await this.discard(vad)
        vad = null
        throw new Error('VAD did not start listening.')
      }
      return true
    } catch (reason) {
      const current = this.isCurrent(revision)
      if (vad) await this.discard(vad)
      if (!current) return false
      throw reason
    }
  }

  private async discard(vad: T): Promise<void> {
    if (this.vad === vad) this.vad = null
    try {
      await vad.destroy()
    } catch {
      // The failed instance must never be reused, even if its cleanup also fails.
    } finally {
      // MicVAD can acquire a MediaStream before it marks itself as listening.
      // Its successful destroy() is therefore not proof that every track stopped.
      this.emergencyStop()
    }
  }
}
