import { writeFile } from 'node:fs/promises'

const SAMPLE_RATE = 16_000
const DURATION_SECONDS = 6

const VOWELS = [
  [730, 1090, 2440],
  [270, 2290, 3010],
  [300, 870, 2240],
  [530, 1840, 2480],
  [570, 840, 2410],
]

function pseudoNoise() {
  let state = 0x51f15e
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0
    return state / 0x1_0000_0000 - 0.5
  }
}

/**
 * Generate a deterministic, non-linguistic formant signal for Chromium's fake
 * microphone. It exists only to exercise the real browser VAD; the E2E test
 * stubs STT at the HTTP boundary and never claims this waveform contains words.
 */
export async function writeSyntheticSpeechLikeWav(filePath) {
  const samples = new Float64Array(SAMPLE_RATE * DURATION_SECONDS)
  const noise = pseudoNoise()
  const speechStart = 0.9
  const syllableDuration = 0.34
  const gapDuration = 0.055
  const stride = syllableDuration + gapDuration
  const syllableCount = 10
  let previousNoise = 0

  for (let index = 0; index < samples.length; index += 1) {
    const time = index / SAMPLE_RATE
    const speechTime = time - speechStart
    if (speechTime < 0) continue
    const syllable = Math.floor(speechTime / stride)
    if (syllable < 0 || syllable >= syllableCount) continue
    const localTime = speechTime - syllable * stride
    if (localTime >= syllableDuration) continue

    const attack = Math.min(1, localTime / 0.035)
    const release = Math.min(1, (syllableDuration - localTime) / 0.055)
    const envelope = Math.max(0, Math.min(attack, release))
    const fundamental = 118 + syllable * 3 + 9 * Math.sin(2 * Math.PI * 2.1 * localTime)
    const formants = VOWELS[syllable % VOWELS.length]
    let voiced = 0

    for (let harmonic = 1; harmonic <= 60; harmonic += 1) {
      const frequency = harmonic * fundamental
      if (frequency >= SAMPLE_RATE / 2) break
      const formantGain = formants.reduce((gain, formant, formantIndex) => {
        const width = 95 + formantIndex * 45
        const distance = (frequency - formant) / width
        return gain + [1, 0.72, 0.42][formantIndex] * Math.exp(-0.5 * distance * distance)
      }, 0.08)
      voiced +=
        (formantGain / harmonic) *
        Math.sin(2 * Math.PI * frequency * time + harmonic * 0.137 * syllable)
    }

    const currentNoise = noise()
    const highPassedNoise = currentNoise - previousNoise * 0.86
    previousNoise = currentNoise
    const consonant = localTime < 0.045 ? highPassedNoise * (1 - localTime / 0.045) * 0.8 : 0
    samples[index] = envelope * (voiced + consonant + currentNoise * 0.025)
  }

  let peak = 0
  for (const sample of samples) peak = Math.max(peak, Math.abs(sample))
  const scale = peak > 0 ? 0.72 / peak : 1
  const output = Buffer.alloc(44 + samples.length * 2)
  output.write('RIFF', 0, 'ascii')
  output.writeUInt32LE(36 + samples.length * 2, 4)
  output.write('WAVE', 8, 'ascii')
  output.write('fmt ', 12, 'ascii')
  output.writeUInt32LE(16, 16)
  output.writeUInt16LE(1, 20)
  output.writeUInt16LE(1, 22)
  output.writeUInt32LE(SAMPLE_RATE, 24)
  output.writeUInt32LE(SAMPLE_RATE * 2, 28)
  output.writeUInt16LE(2, 32)
  output.writeUInt16LE(16, 34)
  output.write('data', 36, 'ascii')
  output.writeUInt32LE(samples.length * 2, 40)
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index] * scale))
    output.writeInt16LE(Math.round(value * (value < 0 ? 32768 : 32767)), 44 + index * 2)
  }
  await writeFile(filePath, output)
}
