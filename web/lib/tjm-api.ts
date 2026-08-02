import { apiFetch, apiUrl } from '@/lib/api'
import {
  normalizeAttemptForClient,
  type TjmAnalytics,
  type TjmAttempt,
  type TjmAttemptItem,
  type TjmAttemptMode,
  type TjmExam,
  type TjmExamInput,
  type TjmImportBatch,
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

export async function listTjmExams(): Promise<TjmExam[]> {
  return (await requestJson<{ exams: TjmExam[] }>('/exams')).exams
}

export function createTjmExam(input: TjmExamInput): Promise<TjmExam> {
  return requestJson('/exams', jsonInit('POST', input))
}

export function activateTjmExam(examId: string): Promise<TjmExam> {
  return requestJson(`/exams/${encodeURIComponent(examId)}/activate`, jsonInit('POST'))
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

export function retireTjmQuestion(versionId: string): Promise<TjmQuestionVersion> {
  return requestJson(`/review/questions/${encodeURIComponent(versionId)}/retire`, jsonInit('POST'))
}

export async function startTjmAttempt(
  examId: string,
  mode: Extract<TjmAttemptMode, 'practice' | 'exam'>
): Promise<TjmAttempt> {
  const attempt = await requestJson<TjmAttempt>(
    '/attempts',
    jsonInit('POST', { exam_id: examId, mode })
  )
  return normalizeAttemptForClient(attempt)
}

export async function getTjmAttempt(attemptId: string): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>(`/attempts/${encodeURIComponent(attemptId)}`)
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
  }
): Promise<TjmAttemptItem> {
  return requestJson(`/attempts/${encodeURIComponent(attemptId)}/answers`, jsonInit('POST', input))
}

export function requestTjmHint(
  attemptId: string,
  position: number,
  elapsedMs: number
): Promise<{ hint: string; hint_number: number }> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/hint`,
    jsonInit('POST', { elapsed_ms: elapsedMs })
  )
}

export function recordTjmVoiceCandidate(
  attemptId: string,
  position: number,
  transcript: string,
  elapsedMs: number
): Promise<TjmVoiceCandidate> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidate`,
    jsonInit('POST', { transcript, elapsed_ms: elapsedMs })
  )
}

export function confirmTjmVoiceCandidate(
  attemptId: string,
  position: number,
  candidateId: number,
  confidence: number | null,
  elapsedMs: number
): Promise<TjmAttemptItem> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidates/${candidateId}/confirm`,
    jsonInit('POST', { confidence, elapsed_ms: elapsedMs })
  )
}

export function cancelTjmVoiceCandidate(
  attemptId: string,
  position: number,
  candidateId: number
): Promise<{ candidate_id: number; status: 'cancelled' }> {
  return requestJson(
    `/attempts/${encodeURIComponent(attemptId)}/items/${position}/voice-candidates/${candidateId}/cancel`,
    jsonInit('POST')
  )
}

export async function submitTjmAttempt(attemptId: string): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>(
      `/attempts/${encodeURIComponent(attemptId)}/submit`,
      jsonInit('POST')
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

export async function startTjmReviewAttempt(examId: string, limit = 20): Promise<TjmAttempt> {
  return normalizeAttemptForClient(
    await requestJson<TjmAttempt>('/review/attempts', jsonInit('POST', { exam_id: examId, limit }))
  )
}

export function getTjmAnalytics(): Promise<TjmAnalytics> {
  return requestJson('/analytics')
}
