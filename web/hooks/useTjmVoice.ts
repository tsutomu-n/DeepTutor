'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MicVAD } from '@ricky0123/vad-web'

import { tjmText } from '@/i18n/tjm'
import { apiFetch, apiUrl } from '@/lib/api'
import { TjmTtsLifecycleController } from '@/lib/tjm-audio-lifecycle'
import { TjmMicrophoneStreamRegistry } from '@/lib/tjm-microphone-lifecycle'
import { TjmVadLifecycleController } from '@/lib/tjm-vad-lifecycle'

export type TjmVoiceState =
  | 'idle'
  | 'loading'
  | 'listening'
  | 'speech'
  | 'transcribing'
  | 'synthesizing'
  | 'speaking'

export const VOICE_REQUEST_TIMEOUT_MS = 15_000

export class TjmVoiceUserError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'TjmVoiceUserError'
  }
}

function writeAscii(view: DataView, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index))
  }
}

export function encodePcm16Wav(samples: Float32Array, sampleRate = 16_000): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, samples.length * 2, true)
  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample))
    view.setInt16(44 + index * 2, clamped < 0 ? clamped * 32768 : clamped * 32767, true)
  })
  return buffer
}

function userError(message: string): TjmVoiceUserError {
  return new TjmVoiceUserError(message)
}

function voiceErrorMessage(reason: unknown, fallback: string): string {
  return reason instanceof TjmVoiceUserError ? reason.message : fallback
}

interface TjmAudioResource {
  audio: HTMLAudioElement
  url: string
}

function disposeAudioResource(resource: TjmAudioResource): void {
  resource.audio.onended = null
  resource.audio.onerror = null
  resource.audio.pause()
  URL.revokeObjectURL(resource.url)
}

export function useTjmVoice(
  onTranscript: (text: string, signal: AbortSignal) => Promise<void> | void
) {
  const [state, setState] = useState<TjmVoiceState>('idle')
  const [error, setError] = useState<string | null>(null)
  const vadLifecycleRef = useRef<TjmVadLifecycleController<MicVAD> | null>(null)
  const vadActiveRef = useRef(false)
  const vadStartRevisionRef = useRef(0)
  const microphoneStreamsRef = useRef(new TjmMicrophoneStreamRegistry<MediaStream>())
  const ttsLifecycleRef = useRef<TjmTtsLifecycleController<TjmAudioResource> | null>(null)
  const ttsAbortRef = useRef<AbortController | null>(null)
  const sttAbortRef = useRef<AbortController | null>(null)
  const sttRevisionRef = useRef(0)
  const mountedRef = useRef(true)
  const onTranscriptRef = useRef(onTranscript)

  useEffect(() => {
    onTranscriptRef.current = onTranscript
  }, [onTranscript])

  const stopMicrophoneStream = useCallback((revision: number, stream: MediaStream) => {
    microphoneStreamsRef.current.stopStream(revision, stream)
  }, [])

  const stopMicrophoneGeneration = useCallback((revision: number) => {
    microphoneStreamsRef.current.stopGeneration(revision)
  }, [])

  const acquireMicrophoneStream = useCallback(
    async (revision: number) => {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          autoGainControl: true,
          noiseSuppression: true,
        },
      })
      microphoneStreamsRef.current.add(revision, stream)
      if (
        !mountedRef.current ||
        !vadActiveRef.current ||
        revision !== vadStartRevisionRef.current
      ) {
        stopMicrophoneStream(revision, stream)
        throw new DOMException('Microphone start was cancelled.', 'AbortError')
      }
      return stream
    },
    [stopMicrophoneStream]
  )

  const createVadLifecycle = useCallback((revision: number) => {
    const lifecycle = new TjmVadLifecycleController<MicVAD>(() =>
      stopMicrophoneGeneration(revision)
    )
    vadLifecycleRef.current = lifecycle
    return lifecycle
  }, [stopMicrophoneGeneration])

  const getTtsLifecycle = useCallback(() => {
    let lifecycle = ttsLifecycleRef.current
    if (!lifecycle) {
      lifecycle = new TjmTtsLifecycleController<TjmAudioResource>(disposeAudioResource)
      ttsLifecycleRef.current = lifecycle
    }
    return lifecycle
  }, [])

  const abortTtsRequest = useCallback(() => {
    const controller = ttsAbortRef.current
    ttsAbortRef.current = null
    controller?.abort()
  }, [])

  const cancelTranscription = useCallback(() => {
    sttRevisionRef.current += 1
    const controller = sttAbortRef.current
    sttAbortRef.current = null
    controller?.abort()
    if (mountedRef.current) setState('idle')
  }, [])

  const releaseVad = useCallback(() => {
    vadActiveRef.current = false
    const releasedRevision = vadStartRevisionRef.current
    vadStartRevisionRef.current += 1
    const lifecycle = vadLifecycleRef.current
    vadLifecycleRef.current = null
    if (lifecycle) {
      lifecycle.cancelStart()
      void lifecycle.destroy().catch(() => undefined)
    }
    stopMicrophoneGeneration(releasedRevision)
  }, [stopMicrophoneGeneration])

  const cancelListeningStart = useCallback(() => {
    releaseVad()
    if (mountedRef.current) setState('idle')
  }, [releaseVad])

  const stopListening = useCallback(async () => {
    releaseVad()
    if (mountedRef.current) setState('idle')
  }, [releaseVad])

  const transcribeSamples = useCallback(
    async (samples: Float32Array) => {
      let controller: AbortController | null = null
      let timeout: number | undefined
      let timedOut = false
      let revision = 0
      try {
        await stopListening()
        revision = ++sttRevisionRef.current
        if (!mountedRef.current) return
        if (mountedRef.current) setState('transcribing')
        const wav = encodePcm16Wav(samples)
        const data = new FormData()
        data.set('file', new Blob([wav], { type: 'audio/wav' }), 'tjm-answer.wav')
        data.set('language', 'ja')
        controller = new AbortController()
        sttAbortRef.current = controller
        timeout = window.setTimeout(() => {
          if (revision !== sttRevisionRef.current) return
          timedOut = true
          controller?.abort()
        }, VOICE_REQUEST_TIMEOUT_MS)
        const response = await apiFetch(apiUrl('/api/v1/voice/stt'), {
          method: 'POST',
          body: data,
          signal: controller.signal,
        })
        if (revision !== sttRevisionRef.current) return
        if (!response.ok) {
          throw userError(tjmText('voice.error.transcriptionHttp', { status: response.status }))
        }
        const body = (await response.json()) as { text?: string }
        const transcript = (body.text || '').trim()
        if (!transcript) throw userError(tjmText('voice.error.noSpeech'))
        await onTranscriptRef.current(transcript, controller.signal)
      } catch (reason) {
        if (mountedRef.current && revision === sttRevisionRef.current) {
          setError(
            timedOut
              ? tjmText('voice.error.transcriptionTimeout')
              : voiceErrorMessage(reason, tjmText('voice.error.transcription'))
          )
        }
      } finally {
        if (timeout !== undefined) window.clearTimeout(timeout)
        if (sttAbortRef.current === controller) sttAbortRef.current = null
        if (mountedRef.current && revision === sttRevisionRef.current) setState('idle')
      }
    },
    [stopListening]
  )

  const startListening = useCallback(async () => {
    setError(null)
    if (!navigator.mediaDevices?.getUserMedia) {
      setError(tjmText('voice.error.microphoneUnsupported'))
      return
    }
    let timeout: number | undefined
    const startRevision = vadStartRevisionRef.current + 1
    vadStartRevisionRef.current = startRevision
    try {
      cancelTranscription()
      abortTtsRequest()
      getTtsLifecycle().stop()
      setState('loading')
      vadActiveRef.current = true
      const lifecycle = createVadLifecycle(startRevision)
      timeout = window.setTimeout(() => {
        if (
          vadLifecycleRef.current !== lifecycle ||
          startRevision !== vadStartRevisionRef.current
        )
          return
        cancelListeningStart()
        if (mountedRef.current) setError(tjmText('voice.error.vadTimeout'))
      }, VOICE_REQUEST_TIMEOUT_MS)
      const listening = await lifecycle.start(async () => {
        const { MicVAD: BrowserMicVAD } = await import('@ricky0123/vad-web')
        return BrowserMicVAD.new({
          model: 'v5',
          startOnLoad: false,
          baseAssetPath: '/vad/',
          onnxWASMBasePath: '/vad/',
          getStream: () => acquireMicrophoneStream(startRevision),
          pauseStream: async stream => stopMicrophoneStream(startRevision, stream),
          resumeStream: async () => acquireMicrophoneStream(startRevision),
          onSpeechStart: () => {
            if (
              mountedRef.current &&
              vadActiveRef.current &&
              startRevision === vadStartRevisionRef.current
            )
              setState('speech')
          },
          onSpeechEnd: samples => {
            if (
              !mountedRef.current ||
              !vadActiveRef.current ||
              startRevision !== vadStartRevisionRef.current
            )
              return
            vadActiveRef.current = false
            void transcribeSamples(samples)
          },
          onVADMisfire: () => {
            if (
              mountedRef.current &&
              vadActiveRef.current &&
              startRevision === vadStartRevisionRef.current
            )
              setState('listening')
          },
        })
      })
      if (
        vadLifecycleRef.current !== lifecycle ||
        startRevision !== vadStartRevisionRef.current
      )
        return
      if (!listening) {
        cancelListeningStart()
        return
      }
      if (mountedRef.current && vadActiveRef.current) setState('listening')
    } catch (reason) {
      if (startRevision === vadStartRevisionRef.current) {
        cancelListeningStart()
        if (mountedRef.current) {
          setError(voiceErrorMessage(reason, tjmText('voice.error.vadStart')))
        }
      }
    } finally {
      if (timeout !== undefined) window.clearTimeout(timeout)
    }
  }, [
    abortTtsRequest,
    acquireMicrophoneStream,
    cancelListeningStart,
    cancelTranscription,
    getTtsLifecycle,
    createVadLifecycle,
    stopMicrophoneStream,
    transcribeSamples,
  ])

  const stopSpeaking = useCallback(() => {
    abortTtsRequest()
    getTtsLifecycle().stop()
    if (mountedRef.current) setState('idle')
  }, [abortTtsRequest, getTtsLifecycle])

  const speak = useCallback(
    async (text: string) => {
      setError(null)
      const lifecycle = getTtsLifecycle()
      abortTtsRequest()
      const token = lifecycle.begin()
      let controller: AbortController | null = null
      let timeout: number | undefined
      let timedOut = false
      try {
        await stopListening()
        if (!mountedRef.current || !lifecycle.isCurrent(token)) return
        setState('synthesizing')
        controller = new AbortController()
        ttsAbortRef.current = controller
        timeout = window.setTimeout(() => {
          if (!lifecycle.isCurrent(token)) return
          timedOut = true
          controller?.abort()
        }, VOICE_REQUEST_TIMEOUT_MS)
        const response = await apiFetch(apiUrl('/api/v1/voice/tts'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
          signal: controller.signal,
        })
        if (!lifecycle.isCurrent(token)) return
        if (!response.ok) {
          throw userError(tjmText('voice.error.synthesisHttp', { status: response.status }))
        }
        const blob = await response.blob()
        if (!lifecycle.isCurrent(token)) return
        const url = URL.createObjectURL(blob)
        const audio = new Audio(url)
        const resource = { audio, url }
        audio.onended = () => {
          if (lifecycle.finish(token, resource) && mountedRef.current) setState('idle')
        }
        audio.onerror = () => {
          if (lifecycle.finish(token, resource) && mountedRef.current) {
            setState('idle')
            setError(tjmText('voice.error.playback'))
          }
        }
        if (!lifecycle.attach(token, resource)) return
        await audio.play()
        if (mountedRef.current && lifecycle.isCurrent(token)) setState('speaking')
      } catch (reason) {
        if (lifecycle.fail(token) && mountedRef.current) {
          setState('idle')
          setError(
            timedOut
              ? tjmText('voice.error.synthesisTimeout')
              : voiceErrorMessage(reason, tjmText('voice.error.synthesis'))
          )
        }
      } finally {
        if (timeout !== undefined) window.clearTimeout(timeout)
        if (ttsAbortRef.current === controller) ttsAbortRef.current = null
      }
    },
    [abortTtsRequest, getTtsLifecycle, stopListening]
  )

  useEffect(() => {
    mountedRef.current = true
    const microphoneStreams = microphoneStreamsRef.current
    return () => {
      mountedRef.current = false
      releaseVad()
      sttRevisionRef.current += 1
      sttAbortRef.current?.abort()
      sttAbortRef.current = null
      ttsAbortRef.current?.abort()
      ttsAbortRef.current = null
      ttsLifecycleRef.current?.stop()
      ttsLifecycleRef.current = null
      microphoneStreams.stopAll()
    }
  }, [releaseVad])

  return {
    state,
    error,
    clearError: () => setError(null),
    startListening,
    cancelListeningStart,
    stopListening,
    cancelTranscription,
    speak,
    stopSpeaking,
  }
}
