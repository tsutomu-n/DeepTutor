/**
 * Owns one TTS resource at a time and invalidates completions from older requests.
 * Fetches themselves are not abortable here; callers discard any response whose
 * token is no longer current before attaching a playable resource.
 */
export class TjmTtsLifecycleController<T> {
  private revision = 0
  private resource: T | null = null

  constructor(private readonly dispose: (resource: T) => void) {}

  begin(): number {
    this.revision += 1
    this.disposeCurrent()
    return this.revision
  }

  isCurrent(token: number): boolean {
    return token === this.revision
  }

  attach(token: number, resource: T): boolean {
    if (!this.isCurrent(token)) {
      this.dispose(resource)
      return false
    }
    this.disposeCurrent()
    this.resource = resource
    return true
  }

  finish(token: number, resource: T): boolean {
    if (!this.isCurrent(token) || this.resource !== resource) return false
    this.revision += 1
    this.disposeCurrent()
    return true
  }

  fail(token: number): boolean {
    if (!this.isCurrent(token)) return false
    this.revision += 1
    this.disposeCurrent()
    return true
  }

  stop(): void {
    this.revision += 1
    this.disposeCurrent()
  }

  private disposeCurrent(): void {
    const current = this.resource
    this.resource = null
    if (current) this.dispose(current)
  }
}
