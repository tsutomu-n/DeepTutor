export interface TjmPendingCommand<T> {
  key: string
  payload: T
}

export class TjmCommandLedger {
  private readonly pending = new Map<string, TjmPendingCommand<unknown>>()

  constructor(
    private readonly createKey: () => string = () => globalThis.crypto.randomUUID(),
    private readonly storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
    private readonly storageKey = 'deeptutor.tjm.pendingCommands'
  ) {
    this.restore()
  }

  begin<T>(scope: string, createPayload: () => T): TjmPendingCommand<T> {
    const existing = this.pending.get(scope)
    if (existing) return existing as TjmPendingCommand<T>
    const command = { key: this.createKey(), payload: createPayload() }
    this.pending.set(scope, command)
    this.persist()
    return command
  }

  complete(scope: string, key: string): void {
    if (this.pending.get(scope)?.key !== key) return
    this.pending.delete(scope)
    this.persist()
  }

  abandon(scope: string): void {
    if (!this.pending.delete(scope)) return
    this.persist()
  }

  clear(): void {
    if (this.pending.size === 0) return
    this.pending.clear()
    this.persist()
  }

  private restore(): void {
    if (!this.storage) return
    try {
      const serialized = this.storage.getItem(this.storageKey)
      if (!serialized) return
      const entries: unknown = JSON.parse(serialized)
      if (!Array.isArray(entries)) return
      for (const entry of entries) {
        if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== 'string') continue
        const command = entry[1]
        if (
          typeof command !== 'object' ||
          command === null ||
          !('key' in command) ||
          typeof command.key !== 'string' ||
          !('payload' in command)
        ) {
          continue
        }
        this.pending.set(entry[0], { key: command.key, payload: command.payload })
      }
    } catch {
      // Storage is a retry aid. Browser privacy/quota failures must not block study.
    }
  }

  private persist(): void {
    if (!this.storage) return
    try {
      if (this.pending.size === 0) {
        this.storage.removeItem(this.storageKey)
        return
      }
      this.storage.setItem(this.storageKey, JSON.stringify([...this.pending.entries()]))
    } catch {
      // Keep the in-memory ledger usable when browser storage is unavailable.
    }
  }
}
