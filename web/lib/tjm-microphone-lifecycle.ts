interface TjmMicrophoneTrack {
  stop(): void
}

interface TjmMicrophoneStream {
  getTracks(): TjmMicrophoneTrack[]
}

export class TjmMicrophoneStreamRegistry<T extends TjmMicrophoneStream> {
  private readonly streamsByGeneration = new Map<number, Set<T>>()

  add(generation: number, stream: T): void {
    let streams = this.streamsByGeneration.get(generation)
    if (!streams) {
      streams = new Set<T>()
      this.streamsByGeneration.set(generation, streams)
    }
    streams.add(stream)
  }

  has(generation: number, stream: T): boolean {
    return this.streamsByGeneration.get(generation)?.has(stream) ?? false
  }

  stopStream(generation: number, stream: T): void {
    stream.getTracks().forEach(track => track.stop())
    const streams = this.streamsByGeneration.get(generation)
    if (!streams) return
    streams.delete(stream)
    if (streams.size === 0) this.streamsByGeneration.delete(generation)
  }

  stopGeneration(generation: number): void {
    const streams = this.streamsByGeneration.get(generation)
    if (!streams) return
    this.streamsByGeneration.delete(generation)
    for (const stream of streams) {
      stream.getTracks().forEach(track => track.stop())
    }
  }

  stopAll(): void {
    const generations = [...this.streamsByGeneration.keys()]
    generations.forEach(generation => this.stopGeneration(generation))
  }
}
