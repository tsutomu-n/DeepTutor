export type TjmAttemptMode = 'practice' | 'exam' | 'review'
export type TjmAttemptStatus = 'in_progress' | 'submitted' | 'expired'
export type TjmQuestionStatus = 'draft' | 'rejected' | 'published' | 'retired'

export interface TjmChoice {
  key: string
  text: string
}

export interface TjmOfficialPassingScoreSource {
  title: string
  publisher: string
  url?: string | null
  published_at?: string | null
}

export interface TjmExam {
  id: string
  title: string
  description: string
  duration_seconds: number
  question_count: number
  official_passing_score: number | null
  official_passing_score_source: TjmOfficialPassingScoreSource | null
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
  blueprint: Record<string, number>
}

export interface TjmExamSnapshot {
  id: string
  title: string
  description: string
  duration_seconds: number
  question_count: number
  snapshot_schema_version?: 2
  maximum_score?: number
  official_passing_score?: number | null
  official_passing_score_source?: TjmOfficialPassingScoreSource | null
  practice_target_score?: number | null
  practice_target_origin?: 'user' | 'legacy_pass_score' | null
  scoring_policy?: {
    type: 'unit_correct'
    version: 1
    points_per_item: 1
  }
  blueprint: Record<string, number>
  revision: number
}

export interface TjmOfficialPassingScoreInput {
  official_passing_score: number | null
  official_passing_score_source: TjmOfficialPassingScoreSource | null
}

export interface TjmExamPreference {
  exam_id: string
  practice_target_score: number | null
  origin: 'user' | 'legacy_pass_score' | null
  updated_at: string | null
}

export interface TjmExamPreferences {
  preferences: TjmExamPreference[]
  total: number
}

export type TjmNotEvaluatedReason =
  | 'official_score_unavailable'
  | 'practice_target_unset'
  | 'mode_not_eligible'
  | 'content_invalidated'
  | 'incomplete_score_scope'
  | 'legacy_score_ambiguous'

export interface TjmOfficialResult {
  status: 'passed' | 'failed' | 'not_evaluated'
  threshold: number | null
  source: TjmOfficialPassingScoreSource | null
  not_evaluated_reason: TjmNotEvaluatedReason | null
}

export interface TjmPracticeTargetResult {
  status: 'achieved' | 'not_achieved' | 'not_evaluated'
  threshold: number | null
  not_evaluated_reason: TjmNotEvaluatedReason | null
}

export interface TjmAttemptResult {
  score: number
  maximum_score: number
  validity: 'eligible' | 'content_invalidated'
  official: TjmOfficialResult
  practice_target: TjmPracticeTargetResult
}

export interface TjmResultDimensionDisplay {
  labelKey:
    | 'result.status.officialPassed'
    | 'result.status.officialFailed'
    | 'result.status.practiceAchieved'
    | 'result.status.practiceNotAchieved'
    | 'result.status.notEvaluated'
  threshold: number | null
  evaluated: boolean
  positive: boolean
  reasonKey: TjmResultReasonKey | null
}

export type TjmResultReasonKey =
  | 'result.reason.noProjection'
  | 'result.reason.officialScoreUnavailable'
  | 'result.reason.practiceTargetUnset'
  | 'result.reason.modeNotEligible'
  | 'result.reason.contentInvalidated'
  | 'result.reason.incompleteScoreScope'
  | 'result.reason.legacyScoreAmbiguous'
  | 'result.reason.unknown'

export interface TjmResultDisplay {
  official: TjmResultDimensionDisplay
  practiceTarget: TjmResultDimensionDisplay
}

export function safeTjmSourceUrl(value: string | null | undefined): string | null {
  if (!value) return null
  if ([...value].some(character => /\s/u.test(character) || character.charCodeAt(0) < 32)) {
    return null
  }
  try {
    const url = new URL(value)
    const isHttp = url.protocol === 'http:' || url.protocol === 'https:'
    return isHttp && url.hostname && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isScore(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0
}

function isThreshold(value: unknown): value is number | null {
  return value === null || isScore(value)
}

function isOfficialSource(value: unknown): value is TjmOfficialPassingScoreSource {
  if (!isRecord(value)) return false
  const allowed = new Set(['title', 'publisher', 'url', 'published_at'])
  if (Object.keys(value).some(key => !allowed.has(key))) return false
  if (typeof value.title !== 'string' || !value.title.trim()) return false
  if (typeof value.publisher !== 'string' || !value.publisher.trim()) return false
  if (
    value.url !== undefined &&
    (typeof value.url !== 'string' || safeTjmSourceUrl(value.url) === null)
  ) {
    return false
  }
  return (
    value.published_at === undefined ||
    (typeof value.published_at === 'string' && Boolean(value.published_at.trim()))
  )
}

const NOT_EVALUATED_REASONS = new Set<TjmNotEvaluatedReason>([
  'official_score_unavailable',
  'practice_target_unset',
  'mode_not_eligible',
  'content_invalidated',
  'incomplete_score_scope',
  'legacy_score_ambiguous',
])

function isNotEvaluatedReason(value: unknown): value is TjmNotEvaluatedReason {
  return typeof value === 'string' && NOT_EVALUATED_REASONS.has(value as TjmNotEvaluatedReason)
}

export function normalizeTjmAttemptResult(
  value: unknown,
  mode?: TjmAttemptMode
): TjmAttemptResult | null {
  if (!isRecord(value)) return null
  if (!isScore(value.score) || !isScore(value.maximum_score) || value.maximum_score <= 0) {
    return null
  }
  if (value.score > value.maximum_score) return null
  if (value.validity !== 'eligible' && value.validity !== 'content_invalidated') return null
  if (!isRecord(value.official) || !isRecord(value.practice_target)) return null

  const official = value.official
  const practiceTarget = value.practice_target
  if (!['passed', 'failed', 'not_evaluated'].includes(String(official.status))) return null
  if (!['achieved', 'not_achieved', 'not_evaluated'].includes(String(practiceTarget.status))) {
    return null
  }
  if (!isThreshold(official.threshold) || !isThreshold(practiceTarget.threshold)) return null
  if (official.source !== null && !isOfficialSource(official.source)) return null
  if ((official.threshold === null) !== (official.source === null)) return null

  if (value.validity === 'content_invalidated') {
    return value as unknown as TjmAttemptResult
  }

  if (official.status === 'not_evaluated') {
    if (!isNotEvaluatedReason(official.not_evaluated_reason)) return null
    if (official.not_evaluated_reason === 'practice_target_unset') return null
    if (
      ['official_score_unavailable', 'legacy_score_ambiguous'].includes(
        official.not_evaluated_reason
      ) &&
      (official.threshold !== null || official.source !== null)
    ) {
      return null
    }
  } else {
    if (
      official.not_evaluated_reason !== null ||
      official.threshold === null ||
      official.source === null ||
      official.threshold > value.maximum_score ||
      (mode !== undefined && mode !== 'exam')
    ) {
      return null
    }
    const passed = value.score >= official.threshold
    if ((official.status === 'passed') !== passed) return null
  }

  if (practiceTarget.status === 'not_evaluated') {
    if (!isNotEvaluatedReason(practiceTarget.not_evaluated_reason)) return null
    if (
      ['official_score_unavailable', 'legacy_score_ambiguous'].includes(
        practiceTarget.not_evaluated_reason
      )
    ) {
      return null
    }
    if (
      practiceTarget.not_evaluated_reason === 'practice_target_unset' &&
      practiceTarget.threshold !== null
    ) {
      return null
    }
  } else {
    if (
      practiceTarget.not_evaluated_reason !== null ||
      practiceTarget.threshold === null ||
      practiceTarget.threshold > value.maximum_score ||
      mode === 'review'
    ) {
      return null
    }
    const achieved = value.score >= practiceTarget.threshold
    if ((practiceTarget.status === 'achieved') !== achieved) return null
  }

  const officialReason = official.not_evaluated_reason
  const targetReason = practiceTarget.not_evaluated_reason
  if (officialReason === 'content_invalidated' || targetReason === 'content_invalidated') return null
  if (officialReason === 'incomplete_score_scope' || targetReason === 'incomplete_score_scope') {
    if (
      official.status !== 'not_evaluated' ||
      practiceTarget.status !== 'not_evaluated' ||
      officialReason !== 'incomplete_score_scope' ||
      targetReason !== 'incomplete_score_scope'
    ) {
      return null
    }
  }
  return value as unknown as TjmAttemptResult
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
  result: TjmAttemptResult | null
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

export function getTjmNotEvaluatedReasonKey(reason: unknown): TjmResultReasonKey {
  switch (reason) {
    case 'official_score_unavailable':
      return 'result.reason.officialScoreUnavailable'
    case 'practice_target_unset':
      return 'result.reason.practiceTargetUnset'
    case 'mode_not_eligible':
      return 'result.reason.modeNotEligible'
    case 'content_invalidated':
      return 'result.reason.contentInvalidated'
    case 'incomplete_score_scope':
      return 'result.reason.incompleteScoreScope'
    case 'legacy_score_ambiguous':
      return 'result.reason.legacyScoreAmbiguous'
    default:
      return 'result.reason.unknown'
  }
}

export function getTjmResultDisplay(result: TjmAttemptResult | null): TjmResultDisplay {
  const normalized = normalizeTjmAttemptResult(result)
  if (normalized === null) {
    const notAvailable: TjmResultDimensionDisplay = {
      labelKey: 'result.status.notEvaluated',
      threshold: null,
      evaluated: false,
      positive: false,
      reasonKey: 'result.reason.noProjection',
    }
    return { official: notAvailable, practiceTarget: { ...notAvailable } }
  }
  result = normalized

  if (result.validity === 'content_invalidated') {
    return {
      official: {
        labelKey: 'result.status.notEvaluated',
        threshold: result.official.threshold,
        evaluated: false,
        positive: false,
        reasonKey: 'result.reason.contentInvalidated',
      },
      practiceTarget: {
        labelKey: 'result.status.notEvaluated',
        threshold: result.practice_target.threshold,
        evaluated: false,
        positive: false,
        reasonKey: 'result.reason.contentInvalidated',
      },
    }
  }

  const official: TjmResultDimensionDisplay = {
    labelKey:
      result.official.status === 'passed'
        ? 'result.status.officialPassed'
        : result.official.status === 'failed'
          ? 'result.status.officialFailed'
          : 'result.status.notEvaluated',
    threshold: result.official.threshold,
    evaluated: result.official.status !== 'not_evaluated',
    positive: result.official.status === 'passed',
    reasonKey:
      result.official.status === 'not_evaluated'
        ? getTjmNotEvaluatedReasonKey(result.official.not_evaluated_reason)
        : null,
  }
  const practiceTarget: TjmResultDimensionDisplay = {
    labelKey:
      result.practice_target.status === 'achieved'
        ? 'result.status.practiceAchieved'
        : result.practice_target.status === 'not_achieved'
          ? 'result.status.practiceNotAchieved'
          : 'result.status.notEvaluated',
    threshold: result.practice_target.threshold,
    evaluated: result.practice_target.status !== 'not_evaluated',
    positive: result.practice_target.status === 'achieved',
    reasonKey:
      result.practice_target.status === 'not_evaluated'
        ? getTjmNotEvaluatedReasonKey(result.practice_target.not_evaluated_reason)
        : null,
  }
  return { official, practiceTarget }
}

/**
 * Keep exam secrecy fail-closed at the browser boundary. Server projections are
 * authoritative; this removes grading fields if a future response regresses.
 */
interface TjmSnapshotScoreFacts {
  legacy: boolean
  maximumScore: number
  officialThreshold: number | null
  officialSource: TjmOfficialPassingScoreSource | null
  practiceTarget: number | null
}

interface TjmFinalItemFacts {
  eligibleCount: number
  invalidatedCount: number
  correctCount: number
}

const SNAPSHOT_V2_FIELDS = new Set([
  'snapshot_schema_version',
  'id',
  'title',
  'description',
  'duration_seconds',
  'question_count',
  'blueprint',
  'revision',
  'maximum_score',
  'official_passing_score',
  'official_passing_score_source',
  'practice_target_score',
  'practice_target_origin',
  'scoring_policy',
])

function isKnownAttemptMode(value: unknown): value is TjmAttemptMode {
  return value === 'practice' || value === 'exam' || value === 'review'
}

function isKnownAttemptStatus(value: unknown): value is TjmAttemptStatus {
  return value === 'in_progress' || value === 'submitted' || value === 'expired'
}

function sameOfficialSource(
  left: TjmOfficialPassingScoreSource | null,
  right: TjmOfficialPassingScoreSource | null
): boolean {
  if (left === null || right === null) return left === right
  return (
    left.title === right.title &&
    left.publisher === right.publisher &&
    (left.url ?? null) === (right.url ?? null) &&
    (left.published_at ?? null) === (right.published_at ?? null)
  )
}

function normalizeSnapshotScoreFacts(
  value: unknown,
  mode: TjmAttemptMode
): TjmSnapshotScoreFacts | null {
  if (!isRecord(value)) return null
  const schemaVersion = value.snapshot_schema_version
  if (schemaVersion === undefined || schemaVersion === null) {
    if (!isScore(value.question_count) || value.question_count <= 0) return null
    const target = value.pass_score === undefined || value.pass_score === null ? null : value.pass_score
    if (!isThreshold(target) || (target !== null && target > value.question_count)) return null
    return {
      legacy: true,
      maximumScore: value.question_count,
      officialThreshold: null,
      officialSource: null,
      practiceTarget: target,
    }
  }
  if (schemaVersion !== 2 || Object.keys(value).some(key => !SNAPSHOT_V2_FIELDS.has(key))) {
    return null
  }
  if (Object.keys(value).length !== SNAPSHOT_V2_FIELDS.size) return null
  if (!isScore(value.question_count) || value.question_count <= 0) return null
  if (
    !isScore(value.maximum_score) ||
    value.maximum_score <= 0 ||
    value.maximum_score > value.question_count
  ) {
    return null
  }
  if (typeof value.id !== 'string' || !value.id.trim()) return null
  if (typeof value.title !== 'string' || !value.title.trim()) return null
  if (typeof value.description !== 'string') return null
  if (!isScore(value.duration_seconds) || value.duration_seconds <= 0) return null
  if (!isScore(value.revision) || value.revision <= 0) return null
  if (!isRecord(value.blueprint)) return null
  const blueprintCounts = Object.entries(value.blueprint)
  if (
    blueprintCounts.some(
      ([area, count]) => !area.trim() || !isScore(count) || (count as number) <= 0
    ) ||
    (blueprintCounts.length > 0 &&
      blueprintCounts.reduce((total, [, count]) => total + (count as number), 0) !==
        value.question_count)
  ) {
    return null
  }
  if (
    !isRecord(value.scoring_policy) ||
    Object.keys(value.scoring_policy).length !== 3 ||
    value.scoring_policy.type !== 'unit_correct' ||
    value.scoring_policy.version !== 1 ||
    value.scoring_policy.points_per_item !== 1
  ) {
    return null
  }

  const officialThreshold = value.official_passing_score
  const officialSource = value.official_passing_score_source
  if (!isThreshold(officialThreshold)) return null
  if ((officialThreshold === null) !== (officialSource === null)) return null
  if (officialSource !== null && !isOfficialSource(officialSource)) return null
  if (
    officialThreshold !== null &&
    (officialThreshold > value.question_count ||
      (mode === 'exam' && officialThreshold > value.maximum_score))
  ) {
    return null
  }

  const practiceTarget = value.practice_target_score
  const targetOrigin = value.practice_target_origin
  if (!isThreshold(practiceTarget)) return null
  if (targetOrigin !== null && targetOrigin !== 'user' && targetOrigin !== 'legacy_pass_score') {
    return null
  }
  if (practiceTarget !== null && targetOrigin === null) return null
  if (practiceTarget === null && targetOrigin === 'legacy_pass_score') return null
  if (
    practiceTarget !== null &&
    (practiceTarget > value.question_count ||
      ((mode === 'exam' || mode === 'practice') && practiceTarget > value.maximum_score))
  ) {
    return null
  }
  return {
    legacy: false,
    maximumScore: value.maximum_score,
    officialThreshold,
    officialSource: officialSource as TjmOfficialPassingScoreSource | null,
    practiceTarget,
  }
}

function collectFinalItemFacts(value: unknown, maximumScore: number): TjmFinalItemFacts | null {
  if (!Array.isArray(value) || value.length !== maximumScore) return null
  const positions = new Set<number>()
  let eligibleCount = 0
  let invalidatedCount = 0
  let correctCount = 0
  for (const item of value) {
    if (!isRecord(item) || !isScore(item.position) || item.position >= maximumScore) return null
    positions.add(item.position)
    const eligibleDisposition =
      item.catalog_disposition === 'current' || item.catalog_disposition === 'superseded'
    const invalidatedDisposition =
      item.catalog_disposition === 'invalid_content' ||
      item.catalog_disposition === 'retired_unclassified'
    if (eligibleDisposition) {
      if (
        item.grading_status !== 'eligible' ||
        typeof item.correct_option_key !== 'string' ||
        typeof item.explanation !== 'string' ||
        typeof item.is_correct !== 'boolean'
      ) {
        return null
      }
      eligibleCount += 1
      correctCount += item.is_correct ? 1 : 0
    } else if (invalidatedDisposition) {
      if (
        item.grading_status !== 'content_invalidated' ||
        'correct_option_key' in item ||
        'explanation' in item ||
        'is_correct' in item
      ) {
        return null
      }
      invalidatedCount += 1
    } else {
      return null
    }
  }
  if (positions.size !== maximumScore) return null
  return { eligibleCount, invalidatedCount, correctCount }
}

function resultMatchesAttemptFacts(
  result: TjmAttemptResult,
  mode: TjmAttemptMode,
  snapshot: TjmSnapshotScoreFacts,
  correctCount: number,
  totalCount: number,
  invalidatedCount: number
): boolean {
  if (
    result.score !== correctCount ||
    result.maximum_score !== snapshot.maximumScore ||
    result.official.threshold !== snapshot.officialThreshold ||
    !sameOfficialSource(result.official.source, snapshot.officialSource) ||
    result.practice_target.threshold !== snapshot.practiceTarget
  ) {
    return false
  }

  const commonReason =
    invalidatedCount > 0
      ? 'content_invalidated'
      : totalCount !== snapshot.maximumScore
        ? 'incomplete_score_scope'
        : null
  if (result.validity !== (invalidatedCount > 0 ? 'content_invalidated' : 'eligible')) return false

  let officialStatus: TjmOfficialResult['status']
  let officialReason: TjmNotEvaluatedReason | null
  let targetStatus: TjmPracticeTargetResult['status']
  let targetReason: TjmNotEvaluatedReason | null
  if (commonReason !== null) {
    officialStatus = 'not_evaluated'
    officialReason = commonReason
    targetStatus = 'not_evaluated'
    targetReason = commonReason
  } else {
    if (mode !== 'exam') {
      officialStatus = 'not_evaluated'
      officialReason = 'mode_not_eligible'
    } else if (snapshot.legacy) {
      officialStatus = 'not_evaluated'
      officialReason = 'legacy_score_ambiguous'
    } else if (snapshot.officialThreshold === null) {
      officialStatus = 'not_evaluated'
      officialReason = 'official_score_unavailable'
    } else {
      officialStatus = correctCount >= snapshot.officialThreshold ? 'passed' : 'failed'
      officialReason = null
    }

    if (mode === 'review') {
      targetStatus = 'not_evaluated'
      targetReason = 'mode_not_eligible'
    } else if (snapshot.practiceTarget === null) {
      targetStatus = 'not_evaluated'
      targetReason = 'practice_target_unset'
    } else {
      targetStatus = correctCount >= snapshot.practiceTarget ? 'achieved' : 'not_achieved'
      targetReason = null
    }
  }
  return (
    result.official.status === officialStatus &&
    result.official.not_evaluated_reason === officialReason &&
    result.practice_target.status === targetStatus &&
    result.practice_target.not_evaluated_reason === targetReason
  )
}

function stripItemGrade(item: TjmAttemptItem): TjmAttemptItem {
  const {
    correct_option_key: _correctOptionKey,
    explanation: _explanation,
    is_correct: _isCorrect,
    ...safe
  } = item
  return safe
}

export function normalizeAttemptForClient(attempt: TjmAttempt): TjmAttempt {
  const knownMode = isKnownAttemptMode(attempt.mode)
  const knownStatus = isKnownAttemptStatus(attempt.status)
  const finalized =
    knownStatus && (attempt.status === 'submitted' || attempt.status === 'expired')
  const snapshot = knownMode ? normalizeSnapshotScoreFacts(attempt.exam_snapshot, attempt.mode) : null
  const itemFacts = finalized && snapshot ? collectFinalItemFacts(attempt.items, snapshot.maximumScore) : null
  let result =
    finalized && knownMode ? normalizeTjmAttemptResult(attempt.result, attempt.mode) : null
  const validFinalFacts =
    finalized &&
    snapshot !== null &&
    itemFacts !== null &&
    isScore(attempt.correct_count) &&
    isScore(attempt.total_count) &&
    isScore(attempt.content_invalidated_count) &&
    attempt.correct_count <= attempt.total_count &&
    attempt.content_invalidated_count === itemFacts.invalidatedCount &&
    (itemFacts.invalidatedCount === 0
      ? attempt.correct_count === itemFacts.correctCount &&
        attempt.total_count === itemFacts.eligibleCount
      : itemFacts.eligibleCount <= attempt.total_count &&
        attempt.total_count <= snapshot.maximumScore &&
        itemFacts.correctCount <= attempt.correct_count &&
        attempt.correct_count <=
          itemFacts.correctCount + (attempt.total_count - itemFacts.eligibleCount))
  if (
    result !== null &&
    (!validFinalFacts ||
      !knownMode ||
      snapshot === null ||
      !resultMatchesAttemptFacts(
        result,
        attempt.mode,
        snapshot,
        attempt.correct_count as number,
        attempt.total_count as number,
        attempt.content_invalidated_count
      ))
  ) {
    result = null
  }
  const normalized = {
    ...attempt,
    result,
  }
  const mustStripGrade =
    !knownMode ||
    !knownStatus ||
    (attempt.mode === 'exam' && !finalized) ||
    (finalized && result === null)
  if (!mustStripGrade) return normalized
  return {
    ...normalized,
    correct_count: null,
    total_count: null,
    result: null,
    items: Array.isArray(normalized.items) ? normalized.items.map(stripItemGrade) : [],
  }
}
