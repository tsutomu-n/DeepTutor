import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.join(webRoot, 'public', 'vad')
const assets = [
  ['node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js', 'vad.worklet.bundle.min.js'],
  ['node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx', 'silero_vad_v5.onnx'],
  ['node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.mjs', 'ort-wasm-simd-threaded.mjs'],
  ['node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.wasm'],
  ['../THIRD_PARTY_NOTICES.md', 'THIRD_PARTY_NOTICES.md'],
]

mkdirSync(output, { recursive: true })
for (const [sourceName, targetName] of assets) {
  const source = path.join(webRoot, sourceName)
  if (!existsSync(source)) {
    throw new Error(`Required VAD asset is missing: ${sourceName}`)
  }
  copyFileSync(source, path.join(output, targetName))
}

console.log(`Prepared ${assets.length} self-hosted VAD assets in public/vad.`)
