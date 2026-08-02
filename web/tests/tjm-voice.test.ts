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
  assert.match(workspace, /Confirm voice answer/)
  assert.match(workspace, /Read question/)
  assert.match(workspace, /stopListening/)
})

test('browser VAD is self-hosted and build assets do not depend on a CDN', () => {
  const hook = source('hooks/useTjmVoice.ts')
  assert.match(hook, /baseAssetPath:\s*['"]\/vad\/['"]/)
  assert.match(hook, /onnxWASMBasePath:\s*['"]\/vad\/['"]/)
  assert.doesNotMatch(hook, /cdn\.jsdelivr|unpkg\.com/)
  assert.match(source('scripts/copy-vad-assets.mjs'), /silero_vad/)
})
