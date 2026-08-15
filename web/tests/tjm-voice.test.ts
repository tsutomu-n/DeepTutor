import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import { encodePcm16Wav } from '../hooks/useTjmVoice'

function source(relativePath: string): string {
  return readFileSync(path.resolve(process.cwd(), relativePath), 'utf8')
}

test('VAD speech samples are encoded as 16 kHz mono PCM WAV', () => {
  const wav = encodePcm16Wav(new Float32Array([-1, 0, 0.5, 1]), 16_000)
  const view = new DataView(wav)
  assert.equal(String.fromCharCode(...new Uint8Array(wav, 0, 4)), 'RIFF')
  assert.equal(String.fromCharCode(...new Uint8Array(wav, 8, 4)), 'WAVE')
  assert.equal(view.getUint16(22, true), 1)
  assert.equal(view.getUint32(24, true), 16_000)
  assert.equal(view.getUint16(34, true), 16)
  assert.equal(view.getInt16(44, true), -32768)
  assert.equal(view.getInt16(50, true), 32767)
})

test('TJM voice path keeps candidate and confirmation as separate requests', () => {
  const api = source('lib/tjm-api.ts')
  assert.match(api, /voice-candidate/)
  assert.match(api, /voice-candidates/)
  assert.match(api, /confirm/)
  const workspace = source('components/tjm/TjmWorkspace.tsx')
  assert.match(workspace, /tjmText\(['"]attempt\.voice\.confirmTitle['"]\)/)
  assert.match(workspace, /tjmText\(['"]attempt\.read\.start['"]\)/)
  assert.match(workspace, /stopListening/)
})

test('browser VAD is self-hosted and build assets do not depend on a CDN', () => {
  const hook = source('hooks/useTjmVoice.ts')
  const assetScript = source('scripts/copy-vad-assets.mjs')
  assert.match(hook, /baseAssetPath:\s*['"]\/vad\/['"]/)
  assert.match(hook, /onnxWASMBasePath:\s*['"]\/vad\/['"]/)
  assert.doesNotMatch(hook, /cdn\.jsdelivr|unpkg\.com/)
  assert.match(assetScript, /silero_vad/)
  assert.match(assetScript, /THIRD_PARTY_NOTICES\.md/)
})

test('voice failures use the fixed Japanese TJM namespace and do not expose raw engine errors', () => {
  const hook = source('hooks/useTjmVoice.ts')
  const workspace = source('components/tjm/TjmWorkspace.tsx')
  assert.match(hook, /TjmVoiceUserError/)
  assert.match(hook, /tjmText\(['"]voice\.error\.microphoneUnsupported['"]\)/)
  assert.match(hook, /tjmText\(['"]voice\.error\.vadStart['"]\)/)
  assert.match(hook, /tjmText\(['"]voice\.error\.transcription['"]\)/)
  assert.match(hook, /tjmText\(['"]voice\.error\.synthesis['"]\)/)
  assert.doesNotMatch(
    hook,
    /Microphone capture is not supported|Transcription failed|Speech synthesis failed|No speech could be recognized/
  )
  assert.match(workspace, /new TjmVoiceUserError\(tjmText\(['"]voice\.questionChanged['"]\)\)/)
  assert.match(workspace, /error instanceof TjmVoiceUserError/)
  assert.doesNotMatch(workspace, /error instanceof Error \? error\.message/)
  assert.match(hook, /TjmTtsLifecycleController/)
  assert.match(hook, /getStream:/)
  assert.match(hook, /pauseStream:/)
  assert.match(hook, /resumeStream:/)
  assert.match(hook, /TjmMicrophoneStreamRegistry/)
  assert.match(hook, /stopMicrophoneGeneration/)
})

test('voice HTTP work is finite and user-cancellable before it can lock screen answers', () => {
  const hook = source('hooks/useTjmVoice.ts')
  const workspace = source('components/tjm/TjmWorkspace.tsx')
  assert.match(hook, /VOICE_REQUEST_TIMEOUT_MS/)
  assert.match(hook, /new AbortController\(\)/)
  assert.match(hook, /signal:\s*controller\.signal/)
  assert.match(hook, /cancelTranscription/)
  assert.match(hook, /cancelListeningStart/)
  assert.match(hook, /['"]synthesizing['"]/)
  assert.match(hook, /voice\.error\.transcriptionTimeout/)
  assert.match(hook, /voice\.error\.synthesisTimeout/)
  assert.match(hook, /voice\.error\.vadTimeout/)
  assert.match(workspace, /attempt\.voice\.cancelTranscription/)
  assert.match(workspace, /attempt\.voice\.cancelStart/)
  assert.match(workspace, /voice\.state === ['"]synthesizing['"]/)
})
