'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MicVAD } from '@ricky0123/vad-web'

import { apiFetch, apiUrl } from '@/lib/api'

export type TjmVoiceState =
  | 'idle'
  | 'loading'
  | 'listening'
  | 'speech'
  | 'transcribing'
  | 'speaking'

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

async function responseError(response: Response, fallback: string): Promise<Error> {
  const body = (await response.json().catch(() => null)) as { detail?: string } | null
  return new Error(body?.detail || `${fallback} (HTTP ${response.status}).`)
}

export function useTjmVoice(onTranscript: (text: string) => Promise<void> | void) {
  const [state, setState] = useState<TjmVoiceState>('idle')
  const [error, setError] = useState<string | null>(null)
  const vadRef = useRef<MicVAD | null>(null)
  const vadActiveRef = useRef(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const audioUrlRef = useRef<string | null>(null)
  const mountedRef = useRef(true)
  const onTranscriptRef = useRef(onTranscript)
  onTranscriptRef.current = onTranscript

  const cleanupAudio = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current)
    audioUrlRef.current = null
  }, [])

  const stopListening = useCallback(async () => {
    vadActiveRef.current = false
    const vad = vadRef.current
    if (vad?.listening) await vad.pause()
    if (mountedRef.current) setState('idle')
  }, [])

  const transcribeSamples = useCallback(
    async (samples: Float32Array) => {
      try {
        await stopListening()
        if (mountedRef.current) setState('transcribing')
        const wav = encodePcm16Wav(samples)
        const data = new FormData()
        data.set('file', new Blob([wav], { type: 'audio/wav' }), 'tjm-answer.wav')
        data.set('language', 'ja')
        const response = await apiFetch(apiUrl('/api/v1/voice/stt'), {
          method: 'POST',
          body: data,
        })
        if (!response.ok) throw await responseError(response, 'Transcription failed')
        const body = (await response.json()) as { text?: string }
        const transcript = (body.text || '').trim()
        if (!transcript) throw new Error('No speech could be recognized. You can answer on screen.')
        await onTranscriptRef.current(transcript)
      } catch (reason) {
        if (mountedRef.current) {
          setError(reason instanceof Error ? reason.message : 'Transcription failed.')
        }
      } finally {
        if (mountedRef.current) setState('idle')
      }
    },
    [stopListening]
  )

  const startListening = useCallback(async () => {
    setError(null)
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Microphone capture is not supported in this browser.')
      return
    }
    try {
      cleanupAudio()
      setState('loading')
      let vad = vadRef.current
      if (!vad) {
        const { MicVAD: BrowserMicVAD } = await import('@ricky0123/vad-web')
        vad = await BrowserMicVAD.new({
          model: 'v5',
          startOnLoad: false,
          baseAssetPath: '/vad/',
          onnxWASMBasePath: '/vad/',
          onSpeechStart: () => {
            if (vadActiveRef.current) setState('speech')
          },
          onSpeechEnd: samples => {
            if (!vadActiveRef.current) return
            vadActiveRef.current = false
            void transcribeSamples(samples)
          },
          onVADMisfire: () => {
            if (vadActiveRef.current) setState('listening')
          },
        })
        vadRef.current = vad
      }
      vadActiveRef.current = true
      await vad.start()
      if (mountedRef.current) setState('listening')
    } catch (reason) {
      vadActiveRef.current = false
      if (mountedRef.current) {
        setState('idle')
        setError(
          reason instanceof Error
            ? reason.message
            : 'Voice activity detection could not start. You can answer on screen.'
        )
      }
    }
  }, [cleanupAudio, transcribeSamples])

  const stopSpeaking = useCallback(() => {
    cleanupAudio()
    setState('idle')
  }, [cleanupAudio])

  const speak = useCallback(
    async (text: string) => {
      setError(null)
      try {
        await stopListening()
        cleanupAudio()
        setState('loading')
        const response = await apiFetch(apiUrl('/api/v1/voice/tts'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        })
        if (!response.ok) throw await responseError(response, 'Speech synthesis failed')
        const url = URL.createObjectURL(await response.blob())
        audioUrlRef.current = url
        const audio = new Audio(url)
        audioRef.current = audio
        audio.onended = stopSpeaking
        audio.onerror = () => {
          setError('The synthesized audio could not be played. You can continue on screen.')
          stopSpeaking()
        }
        await audio.play()
        setState('speaking')
      } catch (reason) {
        cleanupAudio()
        setState('idle')
        setError(reason instanceof Error ? reason.message : 'Speech synthesis failed.')
      }
    },
    [cleanupAudio, stopListening, stopSpeaking]
  )

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      cleanupAudio()
      const vad = vadRef.current
      if (vad) void vad.destroy()
    }
  }, [cleanupAudio])

  return {
    state,
    error,
    clearError: () => setError(null),
    startListening,
    stopListening,
    speak,
    stopSpeaking,
  }
}
