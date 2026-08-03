import { apiFetch, apiUrl } from '@/lib/api'
import {
  normalizeAttemptForClient,
  type TjmAnalytics,
  type TjmAttempt,
  type TjmAttemptItem,
  type TjmAttemptMode,
  type TjmExam,
  type TjmExamInput,
  type TjmExamPreference,
  type TjmExamPreferences,
  type TjmImportBatch,
  type TjmOfficialPassingScoreInput,
  type TjmQuestionInput,
  type TjmQuestionStatus,
  type TjmQuestionVersion,
  type TjmReviewQueueItem,
  type TjmVoiceCandidate,
} from '@/lib/tjm-types'

const BASE = '/api/v1/tjm'

export class TjmApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message)
    this.name = 'TjmApiError'
  }
}

function detailMessage(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (!Array.isArray(value)) return null
  const messages = value.flatMap(entry => {
    if (typeof entry !== 'object' || entry === null || !('msg' in entry)) return []
    return [String((entry as { msg: unknown }).msg)]
  })
  return messages.length ? messages.join('; ') : null
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(apiUrl(`${BASE}${path}`), init)
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? detailMessage((body as { detail: unknown }).detail)
        : null
    throw new TjmApiError(detail ?? `TJM request failed (${response.status})`, response.status)
  }
  return body as T
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }
}

export function tjmCommandInit(method: string, body: unknown, idempotencyKey: string): RequestInit {
  return {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }
}

export async function listTjmExams(): Promise<TjmExam[]> {
  return (await requestJson<{ exams: TjmExam[] }>('/exams')).exams
}

export function createTjmExam(input: TjmExamInput): Promise<TjmExam> {
  return requestJson('/exams', jsonInit('POST', input))
}

export function activateTjmExam(examId: string): Promise<TjmExam> {
  return requestJson(`/exams/${encodeURIComponent(examId)}/activate`, jsonInit('POST'))
}

export function updateTjmOfficialPassingScore(
  examId: string,
  input: TjmOfficialPassingScoreInput
): Promise<TjmExam> {
  return requestJson(
    `/exams/${encodeURIComponent(examId)}/official-passing-score`,
    jsonInit('PUT', input)
  )
}

export function getTjmExamPreferences(): Promise<TjmExamPreferences> {
  return requestJson('/exam-preferences')
}

export function updateTjmExamPreference(
  examId: string,
  practiceTargetScore: number | null
): Promise<TjmExamPreference> {
  return requestJson(
    `/exam-preferences/${encodeURIComponent(examId)}`,
    jsonInit('PUT', { practice_target_score: practiceTargetScore })
  )
}

export function importTjmQuestions(
  file: File,
  importFormat: 'json' | 'jsonl' | 'csv'
): Promise<TjmImportBatch> {
  const data = new FormData()
  data.set('import_format', importFormat)
  data.set('file', file)
  return requestJson('/imports', { method: 'POST', body: data })
}

export function getTjmImportBatch(batchId: string): Promise<TjmImportBatch> {
  return requestJson(`/imports/${encodeURIComponent(batchId)}`)
}

export async function listTjmReviewQuestions(
  status: TjmQuestionStatus = 'draft'
): Promise<TjmQuestionVersion[]> {
  return (
    await requestJson<{ questions: TjmQuestionVersion[] }>(
      `/review/questions?status=${encodeURIComponent(status)}`
    )
  ).questions
}

export function updateTjmDraft(
  versionId: string,
  input: TjmQuestionInput
): Promise<TjmQuestionVersion> {
  return requestJson(`/review/questions/${encodeURIComponent(versionId)}`, jsonInit('PATCH', input))
}

export function reviewTjmQuestion(versionId: string, note: string): Promise<TjmQuestionVersion> {
  return requestJson(
    `/review/questions/${encodeURIComponent(versionId)}/review`,
    jsonInit('POST', { note })
  )
}

export function publishTjmQuestion(versionId: string): Promise<TjmQuestionVersion> {
  return requestJson(`/review/questions/${encodeURIComponent(versionId)}/publish`, jsonInit('POST'))
}

export function rejectTjmQuestion(versionId: string, note: string): Promise<TjmQuestionVersion> {
  return requestJson(
    `/review/questions/${encodeURIComponent(versionId)}/reject`,
    jsonInit('POST', { note })
  )
}

export function retireTjmQuestion(versionId: string, note = ''): Promise<TjmQuestionVersion> {
  return requestJson(
    `/review/questions/${encodeURIComponent(versionId)}/retire`,
    jsonInit('POST', { reason: 'invalid_content', note })
  )
}

export function classifyLegacyTjmRetirement(
  versionId: string,
  replacementQuestionVersionId: string,
  note = ''
): Promise<TjmQuestionVersion> {
  return requestJson(
    `/review/questions/${encodeURIComponent(versionId)}/classify-retirement`,
    jsonInit('POST', {
      reason: 'superseded',
      replacement_question_version_id: replacementQuestionVersionId,
      note,
    })
  )
}

export async function startTjmAttempt(
  examId: string,
  mode: Extract<TjmAttemptMode, 'practice' | 'exam'>,
  idempotencyKey: string
): Promise<TjmAttempt> {
  const attempt = await requestJson<TjmAttempt>(
    '/attempts',
    tjmCommandInit('POST', { exam_id: examId, mode }, idempotencyKey)
  )
  return normalizeAttemptForClient(attempt)
}

export async function getTjmAttempt(attemptId: string): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>(`/attempts/${encodeURIComponent(attemptId)}`)
  )
}

export function openTjmAttemptItem(attemptId: string, position: number): Promise<TjmAttemptItem> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/open`,
    jsonInit('POST')
  )
}

export function recordTjmAnswer(
  attemptId: string,
  input: {
    position: number
    selected_option_key: string
    confidence: number | null
    elapsed_ms: number
    confirmed: boolean
    client_created_at?: string
  },
  idempotencyKey: string
): Promise<TjmAttemptItem> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/answers`,
    tjmCommandInit('POST', input, idempotencyKey)
  )
}

export function requestTjmHint(
  attemptId: string,
  position: number,
  elapsedMs: number,
  idempotencyKey: string
): Promise<{ hint: string; hint_number: number }> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/hint`,
    tjmCommandInit('POST', { elapsed_ms: elapsedMs }, idempotencyKey)
  )
}

export function recordTjmVoiceCandidate(
  attemptId: string,
  position: number,
  transcript: string,
  elapsedMs: number,
  idempotencyKey: string
): Promise<TjmVoiceCandidate> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidate`,
    tjmCommandInit('POST', { transcript, elapsed_ms: elapsedMs }, idempotencyKey)
  )
}

export function confirmTjmVoiceCandidate(
  attemptId: string,
  position: number,
  candidateId: number,
  confidence: number | null,
  elapsedMs: number,
  idempotencyKey: string
): Promise<TjmAttemptItem> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidates/${candidateId}/confirm`,
    tjmCommandInit('POST', { confidence, elapsed_ms: elapsedMs }, idempotencyKey)
  )
}

export function cancelTjmVoiceCandidate(
  attemptId: string,
  position: number,
  candidateId: number,
  idempotencyKey: string
): Promise<{ candidate_id: number; status: 'cancelled' }> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidates/${candidateId}/cancel`,
    tjmCommandInit('POST', undefined, idempotencyKey)
  )
}

export async function submitTjmAttempt(
  attemptId: string,
  idempotencyKey: string
): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>(
      `/attempts/${encodeURIComponent(attemptId)}/submit`,
      tjmCommandInit('POST', undefined, idempotencyKey)
    )
  )
}

export async function getTjmHistory(limit = 100): Promise<TjmAttempt[]> {
  const result = await requestJson<{ attempts: TjmAttempt[] }>(`/history?limit=${limit}`)
  return result.attempts.map(normalizeAttemptForClient)
}

export async function getTjmReviewQueue(): Promise<TjmReviewQueueItem[]> {
  return (await requestJson<{ items: TjmReviewQueueItem[] }>('/review/queue')).items
}

export async function startTjmReviewAttempt(
  examId: string,
  limit: number,
  idempotencyKey: string
): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>(
      '/review/attempts',
      tjmCommandInit('POST', { exam_id: examId, limit }, idempotencyKey)
    )
  )
}

export function getTjmAnalytics(): Promise<TjmAnalytics> {
  return requestJson('/analytics')
}
