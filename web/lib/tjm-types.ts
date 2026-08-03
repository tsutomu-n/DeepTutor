export type TjmAttemptMode = 'practice' | 'exam' | 'review'
export type TjmAttemptStatus = 'in_progress' | 'submitted' | 'expired'
export type TjmQuestionStatus = 'draft' | 'rejected' | 'published' | 'retired'

export interface TjmChoice {
  key: string
  text: string
}

export interface TjmExam {
  id: string
  title: string
  description: string
  duration_seconds: number
  question_count: number
  pass_score: number | null
  blueprint: Record<string, number>
  status: 'draft' | 'active' | 'retired'
  revision: number
  created_by?: string
  created_at?: string
  updated_at?: string
}

export interface TjmExamInput {
  id: string
  title: string
  description: string
  duration_seconds: number
  question_count: number
  pass_score: number | null
  blueprint: Record<string, number>
}

export interface TjmExamSnapshot {
  id: string
  title: string
  description: string
  duration_seconds: number
  question_count: number
  pass_score: number | null
  blueprint: Record<string, number>
  revision: number
}

export interface TjmAttemptItemBase {
  position: number
  question_version_id: string
  stable_id: string
  stem: string
  choices: TjmChoice[]
  area: string
  opened_at: string | null
  answered_at: string | null
  first_presented_at: string | null
  first_answered_at: string | null
  final_answered_at: string | null
  confirmed_option_key: string | null
  confidence: number | null
  elapsed_ms: number | null
  server_elapsed_ms: number | null
  client_active_elapsed_ms: number | null
  hint_count: number
  catalog_disposition:
    | 'unchecked'
    | 'current'
    | 'superseded'
    | 'invalid_content'
    | 'retired_unclassified'
  content_invalidated_at: string | null
  grading_status: 'eligible' | 'content_invalidated'
}

export interface TjmGradedFields {
  correct_option_key: string
  explanation: string
  is_correct: boolean
}

export type TjmAttemptItem = TjmAttemptItemBase & Partial<TjmGradedFields>

export interface TjmAttempt {
  id: string
  exam_id: string
  mode: TjmAttemptMode
  status: TjmAttemptStatus
  exam_snapshot: TjmExamSnapshot
  started_at: string
  deadline_at: string | null
  submitted_at: string | null
  correct_count: number | null
  total_count: number | null
  answered_count: number
  content_invalidated_count: number
  items: TjmAttemptItem[]
}

export interface TjmQuestionVersion {
  id: string
  question_id: string
  exam_id: string
  stable_id: string
  version: number
  stem: string
  choices: TjmChoice[]
  correct_option_key: string
  area: string
  explanation: string
  hints: string[]
  source: Record<string, unknown>
  status: TjmQuestionStatus
  content_revision: number
  created_by: string
  created_at: string
  updated_at: string
  retirement_reason: 'superseded' | 'invalid_content' | null
  retired_at: string | null
  replacement_question_version_id: string | null
  reviewed_by: string | null
  reviewed_revision: number | null
  review_binding_state: 'current' | 'stale' | 'legacy_unverified' | 'unreviewed'
  reviewed_at: string | null
  review_note: string | null
}

export interface TjmQuestionInput {
  exam_id: string
  stable_id: string
  stem: string
  options: TjmChoice[]
  correct_option_key: string
  area: string
  explanation: string
  hints: string[]
  source: Record<string, unknown>
}

export interface TjmImportBatch {
  batch_id: string
  status: 'completed' | 'failed'
  total_rows: number
  imported_rows: number
  duplicate_rows: number
  errors: Array<{ row: number; field: string; message: string }>
}

export interface TjmReviewQueueItem {
  question_version_id: string
  stable_id: string
  exam_id: string
  stem: string
  area: string
  reasons: string[]
  priority: number
  due_at: string | null
}

export interface TjmVoiceCandidate {
  candidate_id: number
  transcript: string
  proposed_option_key: string | null
  elapsed_ms: number
}

export interface TjmMetricGroup {
  total?: number
  answered: number
  correct: number
  accuracy: number | null
}

export interface TjmAnalytics {
  overall: TjmMetricGroup & {
    total: number
    average_elapsed_ms: number | null
    hint_use_rate: number | null
  }
  by_area: Record<string, TjmMetricGroup & { total: number }>
  confidence: Record<string, TjmMetricGroup>
  trend: Array<{
    attempt_id: string
    mode: TjmAttemptMode
    submitted_at: string
    correct: number
    total: number
  }>
}

export function hasGrade(item: TjmAttemptItem): item is TjmAttemptItemBase & TjmGradedFields {
  return (
    typeof item.correct_option_key === 'string' &&
    typeof item.explanation === 'string' &&
    typeof item.is_correct === 'boolean'
  )
}

/**
 * Keep exam secrecy fail-closed at the browser boundary. Server projections are
 * authoritative; this removes grading fields if a future response regresses.
 */
export function normalizeAttemptForClient(attempt: TjmAttempt): TjmAttempt {
  if (attempt.mode !== 'exam' || attempt.status !== 'in_progress') return attempt
  return {
    ...attempt,
    items: attempt.items.map(item => {
      const {
        correct_option_key: _correctOptionKey,
        explanation: _explanation,
        is_correct: _isCorrect,
        ...safe
      } = item
      return safe
    }),
  }
}
