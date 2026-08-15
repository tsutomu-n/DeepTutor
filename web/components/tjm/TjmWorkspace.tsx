'use client'

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  BarChart3,
  BookCheck,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Clock3,
  FileCheck2,
  FileUp,
  Gauge,
  History,
  Lightbulb,
  ListChecks,
  Loader2,
  LockKeyhole,
  Mic,
  RefreshCw,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  Sparkles,
  TimerReset,
  Upload,
  Volume2,
  VolumeX,
} from 'lucide-react'
import Button from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { TjmVoiceUserError, useTjmVoice } from '@/hooks/useTjmVoice'
import { fetchAuthStatus } from '@/lib/auth'
import { adminReviewActions, selectAdminReviewQuestions } from '@/lib/tjm-admin'
import {
  canAnswerTjmItem,
  canNavigateTjmAttempt,
  shouldRefreshExpiredTjmAttempt,
} from '@/lib/tjm-attempt-state'
import {
  activateTjmExam,
  cancelTjmVoiceCandidate,
  classifyLegacyTjmRetirement,
  confirmTjmVoiceCandidate,
  createTjmExam,
  getTjmAnalytics,
  getTjmAttempt,
  getTjmExamPreferences,
  getTjmHistory,
  getTjmReviewQueue,
  importTjmQuestions,
  listTjmExams,
  listTjmReviewQuestions,
  openTjmAttemptItem,
  publishTjmQuestion,
  recordTjmAnswer,
  recordTjmVoiceCandidate,
  requestTjmHint,
  rejectTjmQuestion,
  retireTjmQuestion,
  reviewTjmQuestion,
  startTjmAttempt,
  startTjmReviewAttempt,
  submitTjmAttempt,
  TjmApiError,
  updateTjmExamPreference,
  updateTjmDraft,
  updateTjmOfficialPassingScore,
} from '@/lib/tjm-api'
import { TjmCommandLedger } from '@/lib/tjm-command'
import { TJM_LOCALE, tjmCodeText, tjmText } from '@/i18n/tjm'
import {
  getTjmResultDisplay,
  hasGrade,
  safeTjmSourceUrl,
  type TjmAnalytics,
  type TjmAttempt,
  type TjmAttemptMode,
  type TjmExam,
  type TjmExamInput,
  type TjmExamPreference,
  type TjmExamPreferences,
  type TjmImportBatch,
  type TjmOfficialPassingScoreInput,
  type TjmOfficialPassingScoreSource,
  type TjmQuestionInput,
  type TjmQuestionVersion,
  type TjmReviewQueueItem,
  type TjmVoiceCandidate,
} from '@/lib/tjm-types'

type WorkspaceTab = 'learn' | 'review' | 'insights' | 'admin'
const ACTIVE_ATTEMPT_KEY = 'deeptutor.tjm.activeAttemptId'

const panelClass =
  'rounded-[18px] border border-[var(--border)] bg-[var(--card)] shadow-[0_1px_1px_rgba(0,0,0,0.02)]'
const inputClass =
  'w-full rounded-xl border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm text-[var(--foreground)] outline-none transition focus:border-[var(--ring)] focus:ring-2 focus:ring-[var(--ring)]/15'

function formatDuration(seconds: number): string {
  if (seconds < 60) return tjmText('duration.seconds', { seconds })
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return remainder
    ? tjmText('duration.minutesSeconds', { minutes, seconds: remainder })
    : tjmText('duration.minutes', { minutes })
}

function formatElapsed(milliseconds: number | null): string {
  if (milliseconds === null) return '—'
  const seconds = Math.round(milliseconds / 1000)
  return formatDuration(seconds)
}

function percent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

function messageOf(error: unknown): string {
  if (error instanceof TjmApiError) {
    return tjmText('error.apiRequest', { status: error.status })
  }
  if (error instanceof TjmVoiceUserError) return error.message
  return tjmText('error.unexpected')
}

function formatTjmDate(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('ja-JP').format(parsed)
}

function sessionCommandStorage(): Storage | undefined {
  if (typeof window === 'undefined') return undefined
  try {
    return window.sessionStorage
  } catch {
    return undefined
  }
}

function TabButton({
  active,
  icon: Icon,
  label,
  count,
  onClick,
}: {
  active: boolean
  icon: typeof BookCheck
  label: string
  count?: number
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      className={`flex items-center gap-2 border-b-2 px-1 pb-3 pt-1 text-sm transition ${
        active
          ? 'border-[var(--foreground)] font-semibold text-[var(--foreground)]'
          : 'border-transparent text-[var(--muted-foreground)] hover:text-[var(--foreground)]'
      }`}
    >
      <Icon size={16} strokeWidth={active ? 2 : 1.6} />
      {label}
      {typeof count === 'number' && count > 0 ? (
        <span className="rounded-full bg-[var(--accent)] px-1.5 py-0.5 text-[10px] tabular-nums">
          {count}
        </span>
      ) : null}
    </button>
  )
}

function EmptyPanel({
  icon: Icon,
  title,
  children,
}: {
  icon: typeof BookCheck
  title: string
  children: React.ReactNode
}) {
  return (
    <div
      className={`${panelClass} flex min-h-64 flex-col items-center justify-center px-6 py-12 text-center`}
    >
      <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--secondary)]">
        <Icon size={20} className="text-[var(--muted-foreground)]" />
      </div>
      <h2 className="font-serif text-lg font-semibold text-[var(--foreground)]">{title}</h2>
      <div className="mt-2 max-w-md text-sm leading-6 text-[var(--muted-foreground)]">
        {children}
      </div>
    </div>
  )
}

function ExamCard({
  exam,
  preference,
  busy,
  onStart,
  onUpdateTarget,
}: {
  exam: TjmExam
  preference: TjmExamPreference | undefined
  busy: boolean
  onStart: (exam: TjmExam, mode: 'practice' | 'exam') => void
  onUpdateTarget: (examId: string, target: number | null) => Promise<void>
}) {
  const [targetDraft, setTargetDraft] = useState(
    preference?.practice_target_score === null || preference?.practice_target_score === undefined
      ? ''
      : String(preference.practice_target_score)
  )
  const [targetError, setTargetError] = useState<string | null>(null)

  const saveTarget = async (target: number | null) => {
    setTargetError(null)
    try {
      await onUpdateTarget(exam.id, target)
    } catch (reason) {
      setTargetError(messageOf(reason))
    }
  }

  const submitTarget = async (event: FormEvent) => {
    event.preventDefault()
    if (targetDraft.trim() === '') {
      setTargetError(tjmText('exam.target.required'))
      return
    }
    const target = Number(targetDraft)
    if (!Number.isInteger(target) || target < 0 || target > exam.question_count) {
      setTargetError(tjmText('exam.target.range', { maximum: exam.question_count }))
      return
    }
    await saveTarget(target)
  }

  const scoringUrl = safeTjmSourceUrl(exam.official_passing_score_source?.url)

  return (
    <article className={`${panelClass} overflow-hidden`}>
      <div className="border-b border-[var(--border)] px-5 py-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="rounded-full border border-[var(--border)] bg-[var(--secondary)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
            {tjmText('exam.revision', { revision: exam.revision })}
          </span>
          <span className="text-xs text-[var(--muted-foreground)]">{exam.id}</span>
        </div>
        <h2 className="font-serif text-xl font-semibold tracking-tight text-[var(--foreground)]">
          {exam.title}
        </h2>
        <p className="mt-2 min-h-10 text-sm leading-5 text-[var(--muted-foreground)]">
          {exam.description || tjmText('exam.defaultDescription')}
        </p>
      </div>
      <dl className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] border-b border-[var(--border)] bg-[var(--secondary)]/45 sm:grid-cols-4 sm:divide-y-0">
        <div className="px-4 py-3">
          <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
            {tjmText('exam.field.items')}
          </dt>
          <dd className="mt-1 font-mono text-sm font-semibold">{exam.question_count}</dd>
        </div>
        <div className="px-4 py-3">
          <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
            {tjmText('exam.field.time')}
          </dt>
          <dd className="mt-1 font-mono text-sm font-semibold">
            {formatDuration(exam.duration_seconds)}
          </dd>
        </div>
        <div className="px-4 py-3">
          <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
            {tjmText('exam.field.officialScore')}
          </dt>
          <dd className="mt-1 font-mono text-sm font-semibold">
            {exam.official_passing_score ?? '—'}
          </dd>
        </div>
        <div className="px-4 py-3">
          <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
            {tjmText('exam.field.practiceTarget')}
          </dt>
          <dd className="mt-1 font-mono text-sm font-semibold">
            {preference?.practice_target_score ?? '—'}
          </dd>
        </div>
      </dl>
      {exam.official_passing_score_source ? (
        <p className="border-b border-[var(--border)] px-5 py-3 text-xs leading-5 text-[var(--muted-foreground)]">
          {tjmText('exam.scoringSource')}{' '}
          {scoringUrl ? (
            <a
              href={scoringUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-[var(--foreground)]"
            >
              {exam.official_passing_score_source.title}
            </a>
          ) : (
            exam.official_passing_score_source.title
          )}{' '}
          · {exam.official_passing_score_source.publisher}
        </p>
      ) : (
        <p className="border-b border-[var(--border)] px-5 py-3 text-xs text-[var(--muted-foreground)]">
          {tjmText('exam.scoringSourceUnset')}
        </p>
      )}
      <form
        onSubmit={event => void submitTarget(event)}
        className="flex flex-wrap items-end gap-2 border-b border-[var(--border)] px-5 py-4"
      >
        <label className="grid min-w-40 flex-1 gap-1 text-xs font-medium">
          {tjmText('exam.personalTarget')}
          <input
            type="number"
            min={0}
            max={exam.question_count}
            step={1}
            value={targetDraft}
            disabled={busy}
            onChange={event => setTargetDraft(event.target.value)}
            placeholder={`0–${exam.question_count}`}
            className={inputClass}
          />
        </label>
        <Button type="submit" size="sm" variant="secondary" disabled={busy}>
          {tjmText('exam.target.save')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={
            busy ||
            preference?.practice_target_score === null ||
            preference?.practice_target_score === undefined
          }
          onClick={() => void saveTarget(null)}
        >
          {tjmText('exam.target.clear')}
        </Button>
        {targetError ? (
          <p role="alert" className="w-full text-xs text-[var(--destructive)]">
            {targetError}
          </p>
        ) : null}
      </form>
      <div className="flex flex-wrap gap-2 px-5 py-4">
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          icon={<Sparkles size={15} />}
          onClick={() => onStart(exam, 'practice')}
        >
          {tjmText('exam.start.practice')}
        </Button>
        <Button
          type="button"
          disabled={busy}
          icon={<TimerReset size={15} />}
          onClick={() => onStart(exam, 'exam')}
        >
          {tjmText('exam.start.timed')}
        </Button>
      </div>
    </article>
  )
}

function ExamCards({
  exams,
  preferences,
  busy,
  onStart,
  onUpdateTarget,
}: {
  exams: TjmExam[]
  preferences: TjmExamPreference[]
  busy: boolean
  onStart: (exam: TjmExam, mode: 'practice' | 'exam') => void
  onUpdateTarget: (examId: string, target: number | null) => Promise<void>
}) {
  if (!exams.length) {
    return (
      <EmptyPanel icon={ScrollText} title={tjmText('exam.empty.title')}>
        {tjmText('exam.empty.body')}
      </EmptyPanel>
    )
  }
  const preferenceByExam = new Map(preferences.map(preference => [preference.exam_id, preference]))
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {exams.map(exam => {
        const preference = preferenceByExam.get(exam.id)
        return (
          <ExamCard
            key={`${exam.id}:${preference?.practice_target_score ?? 'none'}:${preference?.origin ?? 'none'}:${preference?.updated_at ?? 'never'}`}
            exam={exam}
            preference={preference}
            busy={busy}
            onStart={onStart}
            onUpdateTarget={onUpdateTarget}
          />
        )
      })}
    </div>
  )
}

function AttemptResultSummary({ attempt, compact = false }: { attempt: TjmAttempt; compact?: boolean }) {
  const display = getTjmResultDisplay(attempt.result)
  const scoringSource = attempt.result?.official.source ?? null
  const scoringUrl = safeTjmSourceUrl(scoringSource?.url)
  const dimensions = [
    ['result.dimension.official', display.official],
    ['result.dimension.practice', display.practiceTarget],
  ] as const

  return (
    <div className={compact ? 'grid gap-2' : 'grid gap-3 p-4'}>
      {dimensions.map(([title, result]) => (
        <div
          key={title}
          className={
            compact
              ? 'min-w-44 text-xs'
              : 'rounded-xl border border-[var(--border)] bg-[var(--secondary)]/35 p-3'
          }
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
              {tjmText(title)}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                !result.evaluated
                  ? 'bg-[var(--secondary)] text-[var(--muted-foreground)]'
                  : result.positive
                    ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                    : 'bg-[var(--destructive)]/10 text-[var(--destructive)]'
              }`}
            >
              {tjmText(result.labelKey)}
            </span>
          </div>
          <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">
            {tjmText('result.threshold', { threshold: result.threshold ?? '—' })}
          </p>
          {result.reasonKey ? (
            <p className="mt-1 text-[11px] leading-4 text-[var(--muted-foreground)]">
              {tjmText(result.reasonKey)}
            </p>
          ) : null}
          {title === 'result.dimension.official' && scoringSource && !compact ? (
            <p className="mt-1 text-[11px] leading-4 text-[var(--muted-foreground)]">
              {tjmText('exam.scoringSource')}{' '}
              {scoringUrl ? (
                <a
                  href={scoringUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline underline-offset-2 hover:text-[var(--foreground)]"
                >
                  {scoringSource.title}
                </a>
              ) : (
                scoringSource.title
              )}{' '}
              · {scoringSource.publisher}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  )
}

function AttemptDesk({
  attempt,
  busy,
  onChange,
  onFinalize,
  onExit,
}: {
  attempt: TjmAttempt
  busy: boolean
  onChange: (attempt: TjmAttempt) => void
  onFinalize: (attempt: TjmAttempt) => void
  onExit: () => void
}) {
  const [position, setPosition] = useState(0)
  const [selected, setSelected] = useState<Record<number, string>>({})
  const [confidence, setConfidence] = useState<Record<number, number>>({})
  const [hints, setHints] = useState<Record<number, string[]>>({})
  const [actionBusy, setActionBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitOpen, setSubmitOpen] = useState(false)
  const [voiceCandidate, setVoiceCandidate] = useState<TjmVoiceCandidate | null>(null)
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null)
  const [openedItems, setOpenedItems] = useState<Record<string, boolean>>({})
  const [openingItems, setOpeningItems] = useState<Record<string, boolean>>({})
  const [openRetry, setOpenRetry] = useState(0)
  const [expiryPoll, setExpiryPoll] = useState(0)
  const openedAtRef = useRef<Record<number, number>>({})
  const expiryRefreshRef = useRef<string | null>(null)
  const openRequestsRef = useRef(
    new Map<string, Promise<Awaited<ReturnType<typeof openTjmAttemptItem>>>>()
  )
  const voiceTargetRef = useRef<{ attemptId: string; position: number } | null>(null)
  const [commandLedger] = useState(
    () =>
      new TjmCommandLedger(
        undefined,
        sessionCommandStorage(),
        `deeptutor.tjm.attemptCommands.${attempt.id}`
      )
  )
  const item = attempt.items[position]
  const finalized = attempt.status !== 'in_progress'
  const resultInvalidated =
    attempt.content_invalidated_count > 0 || attempt.result?.validity === 'content_invalidated'
  const itemScope = `${attempt.id}:${position}`
  const answerScope = `answer:${itemScope}`
  const itemEligible = item?.grading_status !== 'content_invalidated'
  const serverOpened =
    finalized ||
    !itemEligible ||
    Boolean(item?.first_presented_at) ||
    Boolean(openedItems[itemScope])
  const deadlineReached = secondsLeft === 0
  const canAnswer = canAnswerTjmItem({
    status: attempt.status,
    mode: attempt.mode,
    confirmed: Boolean(item?.confirmed_option_key),
    serverOpened,
    secondsLeft,
    gradingStatus: item?.grading_status ?? 'content_invalidated',
  })
  const applyAttempt = useCallback(
    (updated: TjmAttempt) => {
      if (updated.status === 'in_progress') onChange(updated)
      else onFinalize(updated)
    },
    [onChange, onFinalize]
  )
  const elapsedMs = () => Math.max(0, Date.now() - (openedAtRef.current[position] ?? Date.now()))
  const voice = useTjmVoice(async (transcript, signal) => {
    const target = voiceTargetRef.current
    if (!target || target.attemptId !== attempt.id || target.position !== position) {
      throw new TjmVoiceUserError(tjmText('voice.questionChanged'))
    }
    const targetScope = `${target.attemptId}:${target.position}`
    const scope = `voice-candidate:${targetScope}:${transcript}`
    const command = commandLedger.begin(scope, () => ({
      position: target.position,
      transcript,
      elapsedMs: elapsedMs(),
    }))
    const candidate = await recordTjmVoiceCandidate(
      attempt.id,
      command.payload.position,
      command.payload.transcript,
      command.payload.elapsedMs,
      command.key,
      signal
    )
    if (signal.aborted) return
    commandLedger.complete(scope, command.key)
    voiceTargetRef.current = null
    if (candidate.proposed_option_key === null) {
      const cancelScope = `voice-cancel:${attempt.id}:${candidate.candidate_id}`
      const cancelCommand = commandLedger.begin(cancelScope, () => ({
        position: target.position,
        candidateId: candidate.candidate_id,
      }))
      try {
        await cancelTjmVoiceCandidate(
          attempt.id,
          cancelCommand.payload.position,
          cancelCommand.payload.candidateId,
          cancelCommand.key,
          signal
        )
        if (signal.aborted) return
        commandLedger.complete(cancelScope, cancelCommand.key)
      } catch (reason) {
        if (signal.aborted) return
        setVoiceCandidate(candidate)
        throw new TjmVoiceUserError(
          tjmText('voice.discardFailed', { message: messageOf(reason) })
        )
      }
      throw new TjmVoiceUserError(
        tjmText('voice.noChoice', { transcript: candidate.transcript })
      )
    }
    setVoiceCandidate(candidate)
  })
  const { cancelTranscription, stopListening, stopSpeaking } = voice
  const interactionBusy =
    actionBusy || busy || voice.state !== 'idle' || voiceCandidate !== null || submitOpen
  const navigationLocked = !canNavigateTjmAttempt(attempt.status, serverOpened, interactionBusy)

  useEffect(() => {
    if (!(position in openedAtRef.current)) openedAtRef.current[position] = Date.now()
  }, [position])

  // These effects synchronize server presentation, VAD, and deadline resources
  // with attempt transitions. Their state resets are intentional teardown work.
  useEffect(() => {
    if (
      !item ||
      finalized ||
      item.grading_status !== 'eligible' ||
      item.first_presented_at ||
      openedItems[itemScope]
    )
      return
    let active = true
    setOpeningItems(current => ({ ...current, [itemScope]: true }))
    let request = openRequestsRef.current.get(itemScope)
    if (!request) {
      request = openTjmAttemptItem(attempt.id, position)
      openRequestsRef.current.set(itemScope, request)
      void request.then(
        () => openRequestsRef.current.delete(itemScope),
        () => openRequestsRef.current.delete(itemScope)
      )
    }
    void request
      .then(() => {
        if (!active) return
        setOpenedItems(current => ({ ...current, [itemScope]: true }))
        setError(null)
      })
      .catch(async reason => {
        if (!active) return
        if (reason instanceof TjmApiError && reason.status === 409) {
          try {
            const updated = await getTjmAttempt(attempt.id)
            if (active) applyAttempt(updated)
            return
          } catch (refreshError) {
            if (active) {
              setError(
                tjmText('error.refreshAlsoFailed', {
                  message: `${messageOf(reason)} ${messageOf(refreshError)}`,
                })
              )
            }
            return
          }
        }
        setError(tjmText('attempt.timing.startFailed', { message: messageOf(reason) }))
      })
      .finally(() => {
        if (active) setOpeningItems(current => ({ ...current, [itemScope]: false }))
      })
    return () => {
      active = false
    }
  }, [applyAttempt, attempt.id, finalized, item, itemScope, openRetry, openedItems, position])

  useEffect(() => {
    setVoiceCandidate(null)
    voiceTargetRef.current = null
    cancelTranscription()
    void stopListening().catch(reason => setError(messageOf(reason)))
  }, [cancelTranscription, position, stopListening])

  useEffect(() => {
    if (item?.grading_status === 'eligible') return
    voiceTargetRef.current = null
    setVoiceCandidate(null)
    cancelTranscription()
    void stopListening().catch(reason => setError(messageOf(reason)))
  }, [cancelTranscription, item?.grading_status, stopListening])

  useEffect(() => {
    if (!finalized) return
    commandLedger.clear()
    voiceTargetRef.current = null
    setVoiceCandidate(null)
    setSubmitOpen(false)
    cancelTranscription()
    void stopListening().catch(reason => setError(messageOf(reason)))
    stopSpeaking()
  }, [cancelTranscription, commandLedger, finalized, stopListening, stopSpeaking])

  useEffect(() => {
    if (attempt.mode !== 'exam' || !attempt.deadline_at || finalized) {
      setSecondsLeft(null)
      return
    }
    const update = () => {
      setSecondsLeft(Math.max(0, Math.ceil((Date.parse(attempt.deadline_at!) - Date.now()) / 1000)))
    }
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [attempt.deadline_at, attempt.mode, finalized])
  useEffect(() => {
    if (!shouldRefreshExpiredTjmAttempt(attempt.status, attempt.mode, secondsLeft)) return
    const expiryKey = `${attempt.id}:${attempt.deadline_at ?? ''}`
    if (expiryRefreshRef.current === expiryKey) return
    expiryRefreshRef.current = expiryKey
    let active = true
    let retryTimer: number | undefined
    const refreshExpiredAttempt = async () => {
      voiceTargetRef.current = null
      setVoiceCandidate(null)
      setSubmitOpen(false)
      cancelTranscription()
      try {
        await stopListening()
      } catch (reason) {
        if (active) {
          setError(
            tjmText('voice.microphoneDeadlineWarning', { message: messageOf(reason) })
          )
        }
      }
      stopSpeaking()
      try {
        const updated = await getTjmAttempt(attempt.id)
        if (!active) return
        if (updated.status === 'in_progress') {
          expiryRefreshRef.current = null
          retryTimer = window.setTimeout(() => setExpiryPoll(value => value + 1), 1000)
          return
        }
        applyAttempt(updated)
      } catch (reason) {
        if (!active) return
        setError(
          tjmText('voice.deadlineRefreshFailed', { message: messageOf(reason) })
        )
        expiryRefreshRef.current = null
        retryTimer = window.setTimeout(() => setExpiryPoll(value => value + 1), 2000)
      }
    }
    void refreshExpiredAttempt()
    return () => {
      active = false
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [
    attempt.deadline_at,
    attempt.id,
    attempt.mode,
    attempt.status,
    applyAttempt,
    cancelTranscription,
    expiryPoll,
    finalized,
    secondsLeft,
    stopListening,
    stopSpeaking,
  ])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.closest('input, textarea, select, [contenteditable=true]')) return
      if (navigationLocked) return
      const choiceIndex = Number(event.key) - 1
      if (canAnswer && Number.isInteger(choiceIndex) && item?.choices[choiceIndex]) {
        commandLedger.abandon(answerScope)
        setSelected(value => ({ ...value, [position]: item.choices[choiceIndex].key }))
      }
      if (event.key === 'ArrowLeft') setPosition(value => Math.max(0, value - 1))
      if (event.key === 'ArrowRight')
        setPosition(value => Math.min(attempt.items.length - 1, value + 1))
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [
    answerScope,
    attempt.items.length,
    canAnswer,
    commandLedger,
    item,
    navigationLocked,
    position,
  ])

  if (!item) {
    return (
      <EmptyPanel icon={CircleAlert} title={tjmText('attempt.empty.title')}>
        {tjmText('attempt.empty.body')}
      </EmptyPanel>
    )
  }

  const selectedKey = selected[position] ?? item.confirmed_option_key ?? ''
  const confidenceValue = confidence[position] ?? item.confidence ?? 50
  const isConfirmed = item.confirmed_option_key !== null
  const refreshAttempt = async () => {
    const updated = await getTjmAttempt(attempt.id)
    applyAttempt(updated)
    return updated
  }

  const recoverMutationError = async (reason: unknown) => {
    setError(messageOf(reason))
    if (!(reason instanceof TjmApiError) || reason.status !== 409) return
    try {
      const updated = await getTjmAttempt(attempt.id)
      applyAttempt(updated)
    } catch (refreshError) {
      setError(
        tjmText('error.refreshAlsoFailed', {
          message: `${messageOf(reason)} ${messageOf(refreshError)}`,
        })
      )
    }
  }

  const toggleVoiceCapture = async () => {
    if (voice.state === 'loading') {
      voiceTargetRef.current = null
      voice.cancelListeningStart()
      return
    }
    if (voice.state === 'transcribing') {
      voiceTargetRef.current = null
      voice.cancelTranscription()
      return
    }
    if (voice.state === 'listening' || voice.state === 'speech') {
      voiceTargetRef.current = null
      try {
        await voice.stopListening()
      } catch (reason) {
        setError(messageOf(reason))
      }
      return
    }
    voiceTargetRef.current = { attemptId: attempt.id, position }
    await voice.startListening()
  }

  const confirmAnswer = async () => {
    if (!selectedKey || !canAnswer) return
    setActionBusy(true)
    setError(null)
    const command = commandLedger.begin(answerScope, () => ({
      position,
      selected_option_key: selectedKey,
      confidence: confidenceValue,
      elapsed_ms: elapsedMs(),
      confirmed: true,
      client_created_at: new Date().toISOString(),
    }))
    try {
      await recordTjmAnswer(attempt.id, command.payload, command.key)
      await refreshAttempt()
      commandLedger.complete(answerScope, command.key)
    } catch (reason) {
      await recoverMutationError(reason)
    } finally {
      setActionBusy(false)
    }
  }

  const revealHint = async () => {
    if (!canAnswer) return
    setActionBusy(true)
    setError(null)
    const scope = `hint:${itemScope}`
    const command = commandLedger.begin(scope, () => ({
      position,
      elapsedMs: elapsedMs(),
    }))
    try {
      const result = await requestTjmHint(
        attempt.id,
        command.payload.position,
        command.payload.elapsedMs,
        command.key
      )
      setHints(value => {
        const nextHints = [...(value[position] ?? [])]
        nextHints[result.hint_number - 1] = result.hint
        return { ...value, [position]: nextHints }
      })
      await refreshAttempt()
      commandLedger.complete(scope, command.key)
    } catch (reason) {
      await recoverMutationError(reason)
    } finally {
      setActionBusy(false)
    }
  }

  const finalize = async () => {
    setActionBusy(true)
    setError(null)
    const scope = `submit:${attempt.id}`
    const command = commandLedger.begin(scope, () => ({}))
    try {
      voice.cancelTranscription()
      try {
        await voice.stopListening()
      } catch (reason) {
        setError(tjmText('voice.microphoneSubmitWarning', { message: messageOf(reason) }))
      }
      voice.stopSpeaking()
      const result = await submitTjmAttempt(attempt.id, command.key)
      applyAttempt(result)
      commandLedger.complete(scope, command.key)
      setSubmitOpen(false)
    } catch (reason) {
      await recoverMutationError(reason)
      setSubmitOpen(false)
    } finally {
      setActionBusy(false)
    }
  }

  const confirmVoiceAnswer = async () => {
    if (!voiceCandidate || !canAnswer) return
    setActionBusy(true)
    setError(null)
    const scope = `voice-confirm:${attempt.id}:${voiceCandidate.candidate_id}`
    const command = commandLedger.begin(scope, () => ({
      position,
      candidateId: voiceCandidate.candidate_id,
      confidence: confidenceValue,
      elapsedMs: elapsedMs(),
    }))
    try {
      const confirmedItem = await confirmTjmVoiceCandidate(
        attempt.id,
        command.payload.position,
        command.payload.candidateId,
        command.payload.confidence,
        command.payload.elapsedMs,
        command.key
      )
      if (confirmedItem.confirmed_option_key) {
        setSelected(value => ({
          ...value,
          [position]: confirmedItem.confirmed_option_key!,
        }))
      }
      if (confirmedItem.confidence !== null) {
        setConfidence(value => ({ ...value, [position]: confirmedItem.confidence! }))
      }
      await refreshAttempt()
      commandLedger.complete(scope, command.key)
      setVoiceCandidate(null)
    } catch (reason) {
      await recoverMutationError(reason)
    } finally {
      setActionBusy(false)
    }
  }

  const cancelVoiceAnswer = async () => {
    if (!voiceCandidate) return
    const candidate = voiceCandidate
    const scope = `voice-cancel:${attempt.id}:${candidate.candidate_id}`
    const command = commandLedger.begin(scope, () => ({
      position,
      candidateId: candidate.candidate_id,
    }))
    setActionBusy(true)
    setError(null)
    try {
      await cancelTjmVoiceCandidate(
        attempt.id,
        command.payload.position,
        command.payload.candidateId,
        command.key
      )
      commandLedger.complete(scope, command.key)
      setVoiceCandidate(null)
    } catch (reason) {
      await recoverMutationError(reason)
    } finally {
      setActionBusy(false)
    }
  }

  const questionSpeech = [
    item.stem,
    ...item.choices.map((choice, index) =>
      tjmText('voice.optionSpeech', { number: index + 1, text: choice.text })
    ),
  ].join('\n')
  const readingActive = voice.state === 'synthesizing' || voice.state === 'speaking'

  return (
    <div className="grid min-h-[calc(100dvh-12rem)] gap-4 xl:grid-cols-[minmax(0,1fr)_270px]">
      <section className={`${panelClass} flex min-w-0 flex-col overflow-hidden`}>
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--secondary)]/35 px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="rounded-md border border-[var(--border)] bg-[var(--background)] px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider">
              {tjmCodeText('attempt.mode', attempt.mode)}
            </span>
            <span className="truncate text-sm font-medium">{attempt.exam_snapshot.title}</span>
          </div>
          <div className="flex items-center gap-4 font-mono text-xs tabular-nums">
            <span>
              {tjmText('attempt.progress', {
                answered: attempt.answered_count,
                total: attempt.items.length,
              })}
            </span>
            {secondsLeft !== null ? (
              <span
                className={`flex items-center gap-1.5 font-semibold ${secondsLeft < 60 ? 'text-[var(--destructive)]' : ''}`}
                aria-live="polite"
              >
                <Clock3 size={14} /> {formatDuration(secondsLeft)}
              </span>
            ) : null}
          </div>
        </header>

        <div className="flex-1 px-5 py-6 sm:px-8 sm:py-8">
          <div className="mb-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[var(--muted-foreground)]">
                {tjmText('attempt.questionHeading', { number: position + 1, area: item.area })}
              </p>
              <h1 className="mt-3 max-w-3xl font-serif text-[21px] font-semibold leading-[1.55] tracking-[-0.01em] text-[var(--foreground)] sm:text-[24px]">
                {serverOpened ? item.stem : tjmText('attempt.questionOpening')}
              </h1>
            </div>
            {isConfirmed ? (
              <span className="flex shrink-0 items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                <Check size={13} /> {tjmText('attempt.confirmed')}
              </span>
            ) : null}
          </div>

          <div className="mb-5 flex flex-wrap gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={
                !serverOpened ||
                (deadlineReached && !finalized) ||
                voice.state === 'loading' ||
                voice.state === 'transcribing'
              }
              icon={readingActive ? <VolumeX size={15} /> : <Volume2 size={15} />}
              onClick={() =>
                readingActive ? voice.stopSpeaking() : void voice.speak(questionSpeech)
              }
            >
              {readingActive
                ? tjmText('attempt.read.stop')
                : tjmText('attempt.read.start')}
            </Button>
            {hasGrade(item) ? (
              <Button
                type="button"
                variant="ghost"
                disabled={voice.state !== 'idle'}
                icon={<Volume2 size={15} />}
                onClick={() =>
                  void voice.speak(
                    `${
                      item.is_correct
                        ? tjmText('attempt.speech.correct')
                        : tjmText('attempt.speech.correctAnswer', {
                            option: item.correct_option_key,
                          })
                    } ${item.explanation}`
                  )
                }
              >
                {tjmText('attempt.read.result')}
              </Button>
            ) : null}
          </div>

          <div
            className="grid gap-2.5"
            role="radiogroup"
            aria-label={tjmText('aria.answerChoices')}
          >
            {serverOpened
              ? item.choices.map((choice, index) => {
                  const chosen = choice.key === selectedKey
                  const graded = hasGrade(item)
                  const correct = graded && choice.key === item.correct_option_key
                  const wrong = graded && chosen && !correct
                  return (
                    <button
                      key={choice.key}
                      type="button"
                      role="radio"
                      aria-checked={chosen}
                      disabled={!canAnswer || interactionBusy}
                      onClick={() => {
                        commandLedger.abandon(answerScope)
                        setSelected(value => ({ ...value, [position]: choice.key }))
                      }}
                      className={`group flex w-full items-start gap-3 rounded-xl border px-4 py-3.5 text-left text-sm leading-6 transition disabled:cursor-default ${
                        correct
                          ? 'border-emerald-500/60 bg-emerald-500/8'
                          : wrong
                            ? 'border-[var(--destructive)]/50 bg-[var(--destructive)]/5'
                            : chosen
                              ? 'border-[var(--foreground)] bg-[var(--accent)]/65 shadow-sm'
                              : 'border-[var(--border)] bg-[var(--background)] hover:border-[var(--muted-foreground)]/55 hover:bg-[var(--secondary)]/45'
                      }`}
                    >
                      <span
                        className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border font-mono text-[11px] font-semibold ${chosen ? 'border-[var(--foreground)] bg-[var(--foreground)] text-[var(--background)]' : 'border-[var(--border)]'}`}
                      >
                        {index + 1}
                      </span>
                      <span>{choice.text}</span>
                    </button>
                  )
                })
              : Array.from({ length: item.choices.length }, (_, index) => (
                  <div
                    key={`opening-${index}`}
                    aria-hidden="true"
                    className="h-[54px] animate-pulse rounded-xl border border-[var(--border)] bg-[var(--secondary)]/45"
                  />
                ))}
          </div>

          {canAnswer ? (
            <div className="mt-6 rounded-xl border border-[var(--border)] bg-[var(--secondary)]/35 p-4">
              <div className="mb-3 flex items-center justify-between gap-3 text-xs">
                <label
                  htmlFor={`confidence-${position}`}
                  className="font-medium text-[var(--foreground)]"
                >
                  {tjmText('attempt.confidence.label')}
                </label>
                <span className="font-mono font-semibold tabular-nums">{confidenceValue}%</span>
              </div>
              <input
                id={`confidence-${position}`}
                type="range"
                min={0}
                max={100}
                step={10}
                value={confidenceValue}
                disabled={interactionBusy}
                onChange={event => {
                  commandLedger.abandon(answerScope)
                  setConfidence(value => ({
                    ...value,
                    [position]: Number(event.target.value),
                  }))
                }}
                className="w-full accent-[var(--foreground)]"
              />
              <div className="mt-1 flex justify-between text-[10px] text-[var(--muted-foreground)]">
                <span>{tjmText('attempt.confidence.guessing')}</span>
                <span>{tjmText('attempt.confidence.certain')}</span>
              </div>
            </div>
          ) : null}

          {(hints[position] ?? []).map((hint, index) => (
            <div
              key={`${position}-${index}`}
              className="mt-4 flex gap-3 rounded-xl border border-amber-500/25 bg-amber-500/8 p-4 text-sm leading-6"
            >
              <Lightbulb size={17} className="mt-0.5 shrink-0 text-amber-600" />
              <div>
                <strong className="font-medium">
                  {tjmText('attempt.hint.number', { number: index + 1 })}
                </strong>{' '}
                {hint}
              </div>
            </div>
          ))}

          {hasGrade(item) ? (
            <div
              className={`mt-5 rounded-xl border p-4 ${item.is_correct ? 'border-emerald-500/25 bg-emerald-500/8' : 'border-[var(--destructive)]/25 bg-[var(--destructive)]/5'}`}
            >
              <p className="text-sm font-semibold">
                {item.is_correct
                  ? tjmText('attempt.result.correct')
                  : tjmText('attempt.result.correctAnswer', {
                      option: item.correct_option_key,
                    })}
              </p>
              <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
                {item.explanation || tjmText('attempt.result.noExplanation')}
              </p>
            </div>
          ) : null}

          {item.grading_status === 'content_invalidated' ? (
            <div
              role="status"
              className="mt-5 rounded-xl border border-amber-500/25 bg-amber-500/8 p-4 text-sm leading-6"
            >
              {tjmText('attempt.contentInvalidated')}
            </div>
          ) : null}

          {error || voice.error ? (
            <div
              role="alert"
              className="mt-4 flex items-start gap-2 rounded-xl border border-[var(--destructive)]/30 bg-[var(--destructive)]/5 px-4 py-3 text-sm text-[var(--destructive)]"
            >
              <CircleAlert size={16} className="mt-0.5 shrink-0" /> {error || voice.error}
            </div>
          ) : null}
          {!serverOpened && !finalized ? (
            <div className="mt-4 flex items-center gap-3 rounded-xl border border-amber-500/25 bg-amber-500/8 px-4 py-3 text-sm">
              {openingItems[itemScope] ? (
                <>
                  <Loader2 size={16} className="animate-spin" />
                  {tjmText('attempt.timing.starting')}
                </>
              ) : (
                <>
                  <span>{tjmText('attempt.timing.disabled')}</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={() => setOpenRetry(value => value + 1)}
                  >
                    {tjmText('common.retry')}
                  </Button>
                </>
              )}
            </div>
          ) : null}
        </div>

        <footer className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] bg-[var(--card)]/95 px-5 py-4 backdrop-blur sm:px-8">
          <div className="flex gap-2">
            {attempt.mode !== 'exam' && canAnswer ? (
              <Button
                type="button"
                variant="ghost"
                disabled={interactionBusy}
                loading={actionBusy}
                icon={<Lightbulb size={15} />}
                onClick={revealHint}
              >
                {tjmText('attempt.hint.action')}
              </Button>
            ) : null}
            {canAnswer ? (
              <Button
                type="button"
                variant="ghost"
                disabled={
                  actionBusy ||
                  busy ||
                  !['idle', 'loading', 'listening', 'speech', 'transcribing'].includes(
                    voice.state
                  )
                }
                icon={<Mic size={15} />}
                onClick={() => void toggleVoiceCapture()}
              >
                {voice.state === 'speech'
                  ? tjmText('attempt.voice.hearing')
                  : voice.state === 'loading'
                    ? tjmText('attempt.voice.cancelStart')
                  : voice.state === 'listening'
                    ? tjmText('attempt.voice.stopMicrophone')
                    : voice.state === 'transcribing'
                      ? tjmText('attempt.voice.cancelTranscription')
                      : tjmText('attempt.voice.answer')}
              </Button>
            ) : null}
            {canAnswer ? (
              <Button
                type="button"
                disabled={!selectedKey || interactionBusy}
                loading={actionBusy}
                icon={<LockKeyhole size={15} />}
                onClick={confirmAnswer}
              >
                {isConfirmed && attempt.mode === 'exam'
                  ? tjmText('attempt.answer.change')
                  : tjmText('attempt.answer.confirm')}
              </Button>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={position === 0 || navigationLocked}
              icon={<ChevronLeft size={15} />}
              onClick={() => setPosition(value => value - 1)}
            >
              {tjmText('attempt.previous')}
            </Button>
            {position < attempt.items.length - 1 ? (
              <Button
                type="button"
                variant="secondary"
                disabled={navigationLocked}
                icon={<ChevronRight size={15} />}
                onClick={() => setPosition(value => value + 1)}
              >
                {tjmText('attempt.next')}
              </Button>
            ) : finalized ? (
              <Button type="button" variant="secondary" onClick={onExit}>
                {tjmText('attempt.backToExams')}
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                disabled={deadlineReached || navigationLocked}
                icon={<FileCheck2 size={15} />}
                onClick={() => setSubmitOpen(true)}
              >
                {attempt.mode === 'exam'
                  ? tjmText('attempt.submit.exam')
                  : tjmText('attempt.submit.session')}
              </Button>
            )}
          </div>
        </footer>
      </section>

      <aside className={`${panelClass} h-fit overflow-hidden xl:sticky xl:top-4`}>
        {finalized ? (
          <div className="border-b border-[var(--border)] bg-[var(--foreground)] px-5 py-5 text-[var(--background)]">
            <p className="text-[10px] uppercase tracking-[0.16em] opacity-65">
              {resultInvalidated ? tjmText('result.rawScore') : tjmText('result.finalScore')}
            </p>
            <p className="mt-2 font-serif text-3xl font-semibold tabular-nums">
              {attempt.correct_count ?? 0}
              <span className="text-lg opacity-60">
                /{attempt.total_count ?? attempt.items.length}
              </span>
            </p>
            {resultInvalidated ? (
              <p className="mt-2 text-xs leading-5 opacity-75">
                {attempt.content_invalidated_count > 0
                  ? tjmText('result.invalidatedCount', {
                      count: attempt.content_invalidated_count,
                    })
                  : tjmText('result.invalidatedScope')}
              </p>
            ) : null}
          </div>
        ) : null}
        {finalized ? <AttemptResultSummary attempt={attempt} /> : null}
        <div className="p-4">
          <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.15em] text-[var(--muted-foreground)]">
            {tjmText('attempt.answerSheet')}
          </p>
          <div className="grid grid-cols-5 gap-2">
            {attempt.items.map(entry => (
              <button
                key={entry.position}
                type="button"
                disabled={navigationLocked}
                onClick={() => setPosition(entry.position)}
                aria-label={
                  entry.confirmed_option_key
                    ? tjmText('aria.questionAnswered', { number: entry.position + 1 })
                    : tjmText('aria.question', { number: entry.position + 1 })
                }
                className={`aspect-square rounded-lg border font-mono text-xs font-semibold transition ${
                  position === entry.position
                    ? 'border-[var(--foreground)] bg-[var(--foreground)] text-[var(--background)]'
                    : entry.confirmed_option_key
                      ? 'border-[var(--border)] bg-[var(--accent)] text-[var(--foreground)]'
                      : 'border-[var(--border)] text-[var(--muted-foreground)] hover:border-[var(--muted-foreground)]'
                }`}
              >
                {entry.position + 1}
              </button>
            ))}
          </div>
          <div className="mt-4 border-t border-[var(--border)] pt-4 text-xs leading-5 text-[var(--muted-foreground)]">
            <p>
              {tjmText('attempt.keyboard.select')}
            </p>
            <p>
              {tjmText('attempt.keyboard.navigate')}
            </p>
          </div>
          {!finalized ? (
            <button
              type="button"
              disabled={navigationLocked}
              onClick={onExit}
              className="mt-4 text-xs text-[var(--muted-foreground)] underline-offset-4 hover:text-[var(--foreground)] hover:underline"
            >
              {tjmText('attempt.leave')}
            </button>
          ) : null}
        </div>
      </aside>

      <ConfirmDialog
        open={voiceCandidate !== null}
        title={
          voiceCandidate?.proposed_option_key === null
            ? tjmText('attempt.voice.unrecognizedTitle')
            : tjmText('attempt.voice.confirmTitle')
        }
        confirmLabel={
          voiceCandidate?.proposed_option_key === null
            ? tjmText('common.dismiss')
            : tjmText('attempt.answer.confirm')
        }
        cancelLabel={tjmText('common.cancel')}
        closeLabel={tjmText('common.close')}
        busy={actionBusy}
        busyLabel={tjmText('common.saving')}
        onConfirm={() =>
          void (voiceCandidate?.proposed_option_key === null
            ? cancelVoiceAnswer()
            : confirmVoiceAnswer())
        }
        onCancel={() => void cancelVoiceAnswer()}
      >
        {voiceCandidate
          ? voiceCandidate.proposed_option_key === null
            ? tjmText('voice.unrecognizedCandidate', {
                transcript: voiceCandidate.transcript,
              })
            : tjmText('voice.confirmCandidate', {
                transcript: voiceCandidate.transcript,
                option: voiceCandidate.proposed_option_key,
              })
          : ''}
      </ConfirmDialog>

      <ConfirmDialog
        open={submitOpen}
        title={tjmText('attempt.finalize.title')}
        confirmLabel={tjmText('attempt.finalize.action')}
        cancelLabel={tjmText('common.cancel')}
        closeLabel={tjmText('common.close')}
        busy={actionBusy}
        busyLabel={tjmText('common.grading')}
        onConfirm={() => void finalize()}
        onCancel={() => setSubmitOpen(false)}
      >
        {attempt.mode === 'exam'
          ? tjmText('attempt.finalize.examBody', {
              answered: attempt.answered_count,
              total: attempt.items.length,
            })
          : tjmText('attempt.finalize.practiceBody')}
      </ConfirmDialog>
    </div>
  )
}

function ReviewPanel({
  queue,
  exams,
  history,
  busy,
  onStart,
}: {
  queue: TjmReviewQueueItem[]
  exams: TjmExam[]
  history: TjmAttempt[]
  busy: boolean
  onStart: (examId: string) => void
}) {
  const grouped = useMemo(() => {
    const result = new Map<string, TjmReviewQueueItem[]>()
    for (const item of queue) result.set(item.exam_id, [...(result.get(item.exam_id) ?? []), item])
    return result
  }, [queue])

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section className={`${panelClass} overflow-hidden`}>
        <div className="border-b border-[var(--border)] px-5 py-4">
          <h2 className="font-serif text-lg font-semibold">
            {tjmText('review.ledger.title')}
          </h2>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            {tjmText('review.ledger.description')}
          </p>
        </div>
        {!queue.length ? (
          <div className="px-5 py-12 text-center text-sm text-[var(--muted-foreground)]">
            {tjmText('review.empty')}
          </div>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {Array.from(grouped.entries()).map(([examId, items]) => (
              <div key={examId} className="px-5 py-4">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold">
                      {exams.find(exam => exam.id === examId)?.title ?? examId}
                    </p>
                    <p className="text-xs text-[var(--muted-foreground)]">
                      {tjmText('review.questionCount', { count: items.length })}
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy}
                    icon={<RotateCcw size={14} />}
                    onClick={() => onStart(examId)}
                  >
                    {tjmText('review.start')}
                  </Button>
                </div>
                <div className="grid gap-2">
                  {items.map(item => (
                    <div
                      key={item.question_version_id}
                      className="rounded-xl border border-[var(--border)] bg-[var(--secondary)]/35 px-3.5 py-3"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                          {item.area} · {item.stable_id}
                        </span>
                        <span className="font-mono text-[10px]">
                          {tjmText('review.priority', { priority: item.priority })}
                        </span>
                      </div>
                      <p className="mt-1.5 line-clamp-2 text-sm leading-5">{item.stem}</p>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {item.reasons.map(reason => (
                          <span
                            key={reason}
                            className="rounded-full bg-[var(--accent)] px-2 py-0.5 text-[10px]"
                          >
                            {tjmCodeText('review.reason', reason)}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className={`${panelClass} h-fit overflow-hidden`}>
        <div className="border-b border-[var(--border)] px-5 py-4">
          <h2 className="font-serif text-lg font-semibold">
            {tjmText('review.recent.title')}
          </h2>
        </div>
        {!history.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">
            {tjmText('review.recent.empty')}
          </p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {history.slice(0, 8).map(attempt => (
              <div key={attempt.id} className="grid gap-3 px-5 py-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{attempt.exam_snapshot.title}</p>
                    <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]">
                      {tjmCodeText('attempt.mode', attempt.mode)}・
                      {formatTjmDate(attempt.started_at)}
                    </p>
                  </div>
                  <span className="font-mono text-sm font-semibold tabular-nums">
                    {attempt.correct_count ?? '—'}/{attempt.total_count ?? attempt.items.length}
                  </span>
                </div>
                <AttemptResultSummary attempt={attempt} compact />
                {attempt.content_invalidated_count > 0 ||
                attempt.result?.validity === 'content_invalidated' ? (
                  <span className="rounded-full bg-amber-500/10 px-2 py-1 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
                    {tjmText('review.invalidatedBadge')}
                  </span>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function InsightsPanel({ analytics }: { analytics: TjmAnalytics | null }) {
  if (!analytics || analytics.overall.total === 0) {
    return (
      <EmptyPanel icon={BarChart3} title={tjmText('analytics.empty.title')}>
        {tjmText('analytics.empty.body')}
      </EmptyPanel>
    )
  }
  const cards = [
    [
      tjmText('analytics.card.accuracy'),
      percent(analytics.overall.accuracy),
      tjmText('analytics.card.accuracyNote', {
        correct: analytics.overall.correct,
        total: analytics.overall.total,
      }),
      Gauge,
    ],
    [
      tjmText('analytics.card.responseTime'),
      formatElapsed(analytics.overall.average_elapsed_ms),
      tjmText('analytics.card.responseTimeNote'),
      Clock3,
    ],
    [
      tjmText('analytics.card.hintUse'),
      percent(analytics.overall.hint_use_rate),
      tjmText('analytics.card.hintUseNote'),
      Lightbulb,
    ],
    [
      tjmText('analytics.card.attempts'),
      String(analytics.trend.length),
      tjmText('analytics.card.attemptsNote'),
      History,
    ],
  ] as const
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {cards.map(([label, value, note, Icon]) => (
          <article key={label} className={`${panelClass} p-5`}>
            <div className="flex items-center justify-between text-[var(--muted-foreground)]">
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em]">{label}</span>
              <Icon size={16} />
            </div>
            <p className="mt-4 font-serif text-3xl font-semibold tracking-tight tabular-nums">
              {value}
            </p>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">{note}</p>
          </article>
        ))}
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <section className={`${panelClass} overflow-hidden`}>
          <div className="border-b border-[var(--border)] px-5 py-4">
            <h2 className="font-serif text-lg font-semibold">
              {tjmText('analytics.area.title')}
            </h2>
          </div>
          <div className="space-y-4 p-5">
            {Object.entries(analytics.by_area).map(([area, metric]) => (
              <div key={area}>
                <div className="mb-1.5 flex items-center justify-between gap-3 text-xs">
                  <span className="font-medium">{area}</span>
                  <span className="font-mono">
                    {metric.correct}/{metric.total} · {percent(metric.accuracy)}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-[var(--secondary)]">
                  <div
                    className="h-full rounded-full bg-[var(--foreground)]"
                    style={{ width: `${Math.round((metric.accuracy ?? 0) * 100)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </section>
        <section className={`${panelClass} overflow-hidden`}>
          <div className="border-b border-[var(--border)] px-5 py-4">
            <h2 className="font-serif text-lg font-semibold">
              {tjmText('analytics.confidence.title')}
            </h2>
          </div>
          <div className="grid grid-cols-3 divide-x divide-[var(--border)] p-5">
            {Object.entries(analytics.confidence).map(([band, metric]) => (
              <div key={band} className="px-3 text-center first:pl-0 last:pr-0">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                  {tjmCodeText('analytics.confidence', band)}
                </p>
                <p className="mt-2 font-serif text-2xl font-semibold tabular-nums">
                  {percent(metric.accuracy)}
                </p>
                <p className="mt-1 text-[10px] text-[var(--muted-foreground)]">
                  {tjmText('analytics.confidence.answered', { count: metric.answered })}
                </p>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

function QuestionEditor({
  question,
  busy,
  onSave,
  onClose,
}: {
  question: TjmQuestionVersion
  busy: boolean
  onSave: (input: TjmQuestionInput) => void
  onClose: () => void
}) {
  const [stem, setStem] = useState(question.stem)
  const [area, setArea] = useState(question.area)
  const [choices, setChoices] = useState(question.choices)
  const [correct, setCorrect] = useState(question.correct_option_key)
  const [explanation, setExplanation] = useState(question.explanation)
  const [hints, setHints] = useState(question.hints.join('\n'))
  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-[var(--overlay)] px-4 py-8"
      role="dialog"
      aria-modal="true"
      aria-label={tjmText('admin.editor.aria')}
    >
      <form
        className="mx-auto w-full max-w-2xl rounded-[20px] border border-[var(--border)] bg-[var(--card)] p-5 shadow-2xl sm:p-7"
        onSubmit={event => {
          event.preventDefault()
          onSave({
            exam_id: question.exam_id,
            stable_id: question.stable_id,
            stem,
            area,
            options: choices,
            correct_option_key: correct,
            explanation,
            hints: hints
              .split('\n')
              .map(value => value.trim())
              .filter(Boolean),
            source: question.source,
          })
        }}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <p className="text-[10px] uppercase tracking-[0.15em] text-[var(--muted-foreground)]">
              {tjmText('admin.editor.version', {
                stableId: question.stable_id,
                version: question.version,
              })}
            </p>
            <h2 className="mt-1 font-serif text-xl font-semibold">
              {tjmText('admin.editor.title')}
            </h2>
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>
            {tjmText('common.close')}
          </Button>
        </div>
        <div className="grid gap-4">
          <label className="grid gap-1.5 text-xs font-medium">
            {tjmText('admin.editor.stem')}
            <textarea
              required
              value={stem}
              onChange={event => setStem(event.target.value)}
              rows={4}
              className={inputClass}
            />
          </label>
          <label className="grid gap-1.5 text-xs font-medium">
            {tjmText('admin.editor.area')}
            <input
              required
              value={area}
              onChange={event => setArea(event.target.value)}
              className={inputClass}
            />
          </label>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-xs font-medium">
              {tjmText('admin.editor.choices')}
            </legend>
            {choices.map((choice, index) => (
              <div key={choice.key} className="grid grid-cols-[auto_1fr] items-center gap-2">
                <label className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg border border-[var(--border)] font-mono text-xs">
                  <input
                    type="radio"
                    name="correct"
                    value={choice.key}
                    checked={correct === choice.key}
                    onChange={() => setCorrect(choice.key)}
                    aria-label={tjmText('aria.correctChoice', { option: choice.key })}
                    className="sr-only"
                  />
                  {correct === choice.key ? <Check size={15} /> : choice.key}
                </label>
                <input
                  required
                  value={choice.text}
                  onChange={event =>
                    setChoices(current =>
                      current.map((entry, choiceIndex) =>
                        choiceIndex === index ? { ...entry, text: event.target.value } : entry
                      )
                    )
                  }
                  className={inputClass}
                />
              </div>
            ))}
          </fieldset>
          <label className="grid gap-1.5 text-xs font-medium">
            {tjmText('admin.editor.explanation')}
            <textarea
              value={explanation}
              onChange={event => setExplanation(event.target.value)}
              rows={3}
              className={inputClass}
            />
          </label>
          <label className="grid gap-1.5 text-xs font-medium">
            {tjmText('admin.editor.hints')}{' '}
            <span className="font-normal text-[var(--muted-foreground)]">
              {tjmText('admin.editor.onePerLine')}
            </span>
            <textarea
              value={hints}
              onChange={event => setHints(event.target.value)}
              rows={3}
              className={inputClass}
            />
          </label>
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {tjmText('common.cancel')}
          </Button>
          <Button type="submit" loading={busy}>
            {tjmText('admin.editor.save')}
          </Button>
        </div>
      </form>
    </div>
  )
}

function OfficialPassingScoreEditor({
  exam,
  busy,
  onSave,
}: {
  exam: TjmExam
  busy: boolean
  onSave: (input: TjmOfficialPassingScoreInput) => Promise<void>
}) {
  const [score, setScore] = useState(
    exam.official_passing_score === null ? '' : String(exam.official_passing_score)
  )
  const [sourceTitle, setSourceTitle] = useState(exam.official_passing_score_source?.title ?? '')
  const [publisher, setPublisher] = useState(
    exam.official_passing_score_source?.publisher ?? ''
  )
  const [url, setUrl] = useState(exam.official_passing_score_source?.url ?? '')
  const [publishedAt, setPublishedAt] = useState(
    exam.official_passing_score_source?.published_at ?? ''
  )
  const [error, setError] = useState<string | null>(null)

  const save = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (score.trim() === '') {
      await onSave({
        official_passing_score: null,
        official_passing_score_source: null,
      })
      return
    }

    const threshold = Number(score)
    if (!Number.isInteger(threshold) || threshold < 0 || threshold > exam.question_count) {
      setError(tjmText('admin.score.rangeError', { maximum: exam.question_count }))
      return
    }
    if (!sourceTitle.trim() || !publisher.trim()) {
      setError(tjmText('admin.score.sourceRequired'))
      return
    }
    const normalizedUrl = url.trim() ? safeTjmSourceUrl(url.trim()) : null
    if (url.trim() && normalizedUrl === null) {
      setError(tjmText('admin.score.urlError'))
      return
    }

    const source: TjmOfficialPassingScoreSource = {
      title: sourceTitle.trim(),
      publisher: publisher.trim(),
      ...(normalizedUrl ? { url: normalizedUrl } : {}),
      ...(publishedAt ? { published_at: publishedAt } : {}),
    }
    await onSave({
      official_passing_score: threshold,
      official_passing_score_source: source,
    })
  }

  return (
    <form
      onSubmit={event => void save(event)}
      className="mt-3 grid gap-2 rounded-xl border border-[var(--border)] bg-[var(--secondary)]/25 p-3 sm:grid-cols-2"
    >
      <label className="grid gap-1 text-[11px] font-medium">
        {tjmText('admin.score.label')}
        <input
          type="number"
          min={0}
          max={exam.question_count}
          step={1}
          value={score}
          disabled={busy}
          onChange={event => setScore(event.target.value)}
          className={inputClass}
          placeholder={tjmText('common.notConfigured')}
        />
      </label>
      <label className="grid gap-1 text-[11px] font-medium">
        {tjmText('admin.score.sourceTitle')}
        <input
          value={sourceTitle}
          disabled={busy}
          onChange={event => setSourceTitle(event.target.value)}
          className={inputClass}
        />
      </label>
      <label className="grid gap-1 text-[11px] font-medium">
        {tjmText('admin.score.publisher')}
        <input
          value={publisher}
          disabled={busy}
          onChange={event => setPublisher(event.target.value)}
          className={inputClass}
        />
      </label>
      <label className="grid gap-1 text-[11px] font-medium">
        {tjmText('admin.score.url')}
        <input
          type="url"
          value={url}
          disabled={busy}
          onChange={event => setUrl(event.target.value)}
          className={inputClass}
          placeholder={tjmText('admin.score.urlPlaceholder')}
        />
      </label>
      <label className="grid gap-1 text-[11px] font-medium">
        {tjmText('admin.score.publishedDate')}
        <input
          type="date"
          value={publishedAt}
          disabled={busy}
          onChange={event => setPublishedAt(event.target.value)}
          className={inputClass}
        />
      </label>
      <div className="flex flex-wrap items-end gap-2">
        <Button type="submit" size="sm" variant="secondary" disabled={busy}>
          {tjmText('admin.score.save')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy || exam.official_passing_score === null}
          onClick={() =>
            void onSave({
              official_passing_score: null,
              official_passing_score_source: null,
            })
          }
        >
          {tjmText('admin.score.clear')}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="text-xs text-[var(--destructive)] sm:col-span-2">
          {error}
        </p>
      ) : null}
    </form>
  )
}

function AdminPanel({
  exams,
  questions,
  busy,
  onRefresh,
}: {
  exams: TjmExam[]
  questions: TjmQuestionVersion[]
  busy: boolean
  onRefresh: () => Promise<void>
}) {
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [localBusy, setLocalBusy] = useState(false)
  const [editing, setEditing] = useState<TjmQuestionVersion | null>(null)
  const [importResult, setImportResult] = useState<TjmImportBatch | null>(null)
  const [examForm, setExamForm] = useState({
    id: '',
    title: '',
    description: '',
    durationMinutes: '',
    questionCount: '',
    blueprint: '{}',
  })

  const act = async (action: () => Promise<unknown>, success: string) => {
    setLocalBusy(true)
    setError(null)
    setNotice(null)
    try {
      await action()
      setNotice(success)
      await onRefresh()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setLocalBusy(false)
    }
  }

  const createExam = async (event: FormEvent) => {
    event.preventDefault()
    let blueprint: Record<string, number>
    try {
      const parsed: unknown = JSON.parse(examForm.blueprint)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error()
      blueprint = Object.fromEntries(
        Object.entries(parsed).map(([key, value]) => [key, Number(value)])
      )
    } catch {
      setError(tjmText('admin.exam.blueprintError'))
      return
    }
    const input: TjmExamInput = {
      id: examForm.id,
      title: examForm.title,
      description: examForm.description,
      duration_seconds: Number(examForm.durationMinutes) * 60,
      question_count: Number(examForm.questionCount),
      blueprint,
    }
    await act(() => createTjmExam(input), tjmText('admin.exam.created'))
  }

  const importQuestions = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const file = form.get('file')
    const format = form.get('format')
    if (!(file instanceof File) || !file.size || !['json', 'jsonl', 'csv'].includes(String(format)))
      return
    setLocalBusy(true)
    setError(null)
    try {
      const result = await importTjmQuestions(file, format as 'json' | 'jsonl' | 'csv')
      setImportResult(result)
      setNotice(
        tjmText('admin.import.notice', {
          batchId: result.batch_id,
          imported: result.imported_rows,
          duplicates: result.duplicate_rows,
        })
      )
      await onRefresh()
      event.currentTarget.reset()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setLocalBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <form onSubmit={createExam} className={`${panelClass} p-5`}>
          <div className="mb-4 flex items-center gap-2">
            <ShieldCheck size={17} />
            <h2 className="font-serif text-lg font-semibold">{tjmText('admin.exam.title')}</h2>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1 text-xs font-medium">
              {tjmText('admin.exam.id')}
              <input
                required
                value={examForm.id}
                onChange={event => setExamForm({ ...examForm, id: event.target.value })}
                placeholder={tjmText('admin.exam.idPlaceholder')}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              {tjmText('admin.exam.name')}
              <input
                required
                value={examForm.title}
                onChange={event => setExamForm({ ...examForm, title: event.target.value })}
                placeholder={tjmText('admin.exam.titlePlaceholder')}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium sm:col-span-2">
              {tjmText('admin.exam.description')}
              <input
                value={examForm.description}
                onChange={event => setExamForm({ ...examForm, description: event.target.value })}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              {tjmText('admin.exam.duration')}
              <input
                required
                min={1}
                type="number"
                value={examForm.durationMinutes}
                onChange={event =>
                  setExamForm({ ...examForm, durationMinutes: event.target.value })
                }
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              {tjmText('admin.exam.questionCount')}
              <input
                required
                min={1}
                type="number"
                value={examForm.questionCount}
                onChange={event => setExamForm({ ...examForm, questionCount: event.target.value })}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              {tjmText('admin.exam.blueprint')}
              <input
                required
                value={examForm.blueprint}
                onChange={event => setExamForm({ ...examForm, blueprint: event.target.value })}
                className={`${inputClass} font-mono`}
              />
            </label>
          </div>
          <Button type="submit" className="mt-4" loading={localBusy || busy}>
            {tjmText('admin.exam.create')}
          </Button>
        </form>

        <form onSubmit={importQuestions} className={`${panelClass} p-5`}>
          <div className="mb-4 flex items-center gap-2">
            <FileUp size={17} />
            <h2 className="font-serif text-lg font-semibold">{tjmText('admin.import.title')}</h2>
          </div>
          <p className="mb-4 text-sm leading-6 text-[var(--muted-foreground)]">
            {tjmText('admin.import.description')}
          </p>
          <label className="mb-3 grid gap-1 text-xs font-medium">
            {tjmText('admin.import.format')}
            <select name="format" className={inputClass} defaultValue="json">
              <option value="json">{tjmText('admin.import.jsonArray')}</option>
              <option value="jsonl">{tjmText('admin.import.jsonLines')}</option>
              <option value="csv">{tjmText('admin.import.csv')}</option>
            </select>
          </label>
          <label className="grid cursor-pointer gap-2 rounded-xl border border-dashed border-[var(--border)] bg-[var(--secondary)]/35 px-4 py-6 text-center text-sm hover:border-[var(--muted-foreground)]">
            <Upload size={20} className="mx-auto text-[var(--muted-foreground)]" />
            <span>{tjmText('admin.import.selectFile')}</span>
            <input
              required
              name="file"
              type="file"
              accept=".json,.jsonl,.csv,application/json,text/csv"
              className="mx-auto max-w-full text-xs"
            />
          </label>
          <Button type="submit" className="mt-4" loading={localBusy || busy}>
            {tjmText('admin.import.submit')}
          </Button>
          {importResult ? (
            <p className="mt-3 font-mono text-[10px] text-[var(--muted-foreground)]">
              {tjmText('admin.import.batch', {
                batchId: importResult.batch_id,
                status: tjmCodeText('admin.importStatus', importResult.status),
              })}
            </p>
          ) : null}
        </form>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-xl border border-[var(--destructive)]/30 bg-[var(--destructive)]/5 px-4 py-3 text-sm text-[var(--destructive)]"
        >
          {error}
        </div>
      ) : null}
      {notice ? (
        <div
          role="status"
          className="rounded-xl border border-emerald-500/25 bg-emerald-500/8 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300"
        >
          {notice}
        </div>
      ) : null}

      <section className={`${panelClass} overflow-hidden`}>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div>
            <h2 className="font-serif text-lg font-semibold">{tjmText('admin.catalog.title')}</h2>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              {tjmText('admin.catalog.description')}
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            icon={<RefreshCw size={14} />}
            onClick={() => void onRefresh()}
          >
            {tjmText('common.refresh')}
          </Button>
        </div>
        {!exams.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">
            {tjmText('admin.catalog.empty')}
          </p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {exams.map(exam => (
              <article key={exam.id} className="px-5 py-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">{exam.title}</p>
                    <p className="text-[11px] text-[var(--muted-foreground)]">
                      {tjmText('admin.catalog.summary', {
                        id: exam.id,
                        questions: exam.question_count,
                        duration: formatDuration(exam.duration_seconds),
                        score: exam.official_passing_score ?? '—',
                      })}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="rounded-full bg-[var(--secondary)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider">
                      {tjmCodeText('admin.examStatus', exam.status)}
                    </span>
                    {exam.status === 'draft' ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        loading={localBusy}
                        onClick={() =>
                          void act(
                            () => activateTjmExam(exam.id),
                            tjmText('admin.catalog.activated', { title: exam.title })
                          )
                        }
                      >
                        {tjmText('admin.catalog.activate')}
                      </Button>
                    ) : null}
                  </div>
                </div>
                {exam.status === 'draft' || exam.status === 'active' ? (
                  <OfficialPassingScoreEditor
                    key={[
                      exam.id,
                      exam.official_passing_score ?? 'none',
                      exam.official_passing_score_source?.title ?? '',
                      exam.official_passing_score_source?.publisher ?? '',
                      exam.official_passing_score_source?.url ?? '',
                      exam.official_passing_score_source?.published_at ?? '',
                    ].join(':')}
                    exam={exam}
                    busy={localBusy || busy}
                    onSave={input =>
                      act(
                        () => updateTjmOfficialPassingScore(exam.id, input),
                        tjmText('admin.catalog.scoreSaved', { title: exam.title })
                      )
                    }
                  />
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className={`${panelClass} overflow-hidden`}>
        <div className="border-b border-[var(--border)] px-5 py-4">
          <h2 className="font-serif text-lg font-semibold">{tjmText('admin.review.title')}</h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            {tjmText('admin.review.description')}
          </p>
        </div>
        {!questions.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">
            {tjmText('admin.review.empty')}
          </p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {questions.map(question => (
              <article key={question.id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                      {tjmText('admin.review.questionMeta', {
                        examId: question.exam_id,
                        area: question.area,
                        stableId: question.stable_id,
                        version: question.version,
                      })}
                    </p>
                    <p className="mt-2 text-sm leading-6">{question.stem}</p>
                  </div>
                  <span
                    className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${question.reviewed_by ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'bg-amber-500/10 text-amber-700 dark:text-amber-300'}`}
                  >
                    {question.review_binding_state === 'legacy_unverified'
                      ? tjmText('admin.review.state.legacy_unverified')
                      : question.reviewed_by
                        ? tjmText('admin.review.state.reviewed')
                        : tjmText('admin.review.state.unreviewed')}
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {adminReviewActions(question).canEdit ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => setEditing(question)}
                    >
                      {tjmText('admin.review.action.edit')}
                    </Button>
                  ) : null}
                  {adminReviewActions(question).canReview ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={localBusy}
                      onClick={() =>
                        void act(
                          () => reviewTjmQuestion(question.id, tjmText('admin.review.note.reviewed')),
                          tjmText('admin.review.notice.reviewed')
                        )
                      }
                    >
                      {tjmText('admin.review.action.markReviewed')}
                    </Button>
                  ) : adminReviewActions(question).canPublish ? (
                    <Button
                      type="button"
                      size="sm"
                      loading={localBusy}
                      onClick={() =>
                        void act(
                          () => publishTjmQuestion(question.id),
                          tjmText('admin.review.notice.published')
                        )
                      }
                    >
                      {tjmText('admin.review.action.publish')}
                    </Button>
                  ) : null}
                  {adminReviewActions(question).canReject ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      loading={localBusy}
                      onClick={() =>
                        void act(
                          () => rejectTjmQuestion(question.id, tjmText('admin.review.note.rejected')),
                          tjmText('admin.review.notice.rejected')
                        )
                      }
                    >
                      {tjmText('admin.review.action.reject')}
                    </Button>
                  ) : null}
                  {adminReviewActions(question).canRetire ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      loading={localBusy}
                      onClick={() => {
                        const note = window.prompt(
                          tjmText('admin.review.prompt.invalidate')
                        )
                        if (!note?.trim()) return
                        void act(
                          () => retireTjmQuestion(question.id, note.trim()),
                          tjmText('admin.review.notice.invalidated')
                        )
                      }}
                    >
                      {tjmText('admin.review.action.invalidate')}
                    </Button>
                  ) : null}
                  {adminReviewActions(question).canClassifySuperseded ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={localBusy}
                      onClick={() => {
                        const replacementId = window.prompt(
                          tjmText('admin.review.prompt.replacement')
                        )
                        if (!replacementId?.trim()) return
                        const note =
                          window.prompt(tjmText('admin.review.prompt.auditNote')) ?? ''
                        void act(
                          () =>
                            classifyLegacyTjmRetirement(
                              question.id,
                              replacementId.trim(),
                              note.trim()
                            ),
                          tjmText('admin.review.notice.superseded')
                        )
                      }}
                    >
                      {tjmText('admin.review.action.classifySuperseded')}
                    </Button>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {editing ? (
        <QuestionEditor
          question={editing}
          busy={localBusy}
          onClose={() => setEditing(null)}
          onSave={input =>
            void act(
              () => updateTjmDraft(editing.id, input),
              tjmText('admin.review.notice.updated')
            ).then(() => setEditing(null))
          }
        />
      ) : null}
    </div>
  )
}

export default function TjmWorkspace() {
  const [tab, setTab] = useState<WorkspaceTab>('learn')
  const [exams, setExams] = useState<TjmExam[]>([])
  const [preferences, setPreferences] = useState<TjmExamPreferences>({
    preferences: [],
    total: 0,
  })
  const [history, setHistory] = useState<TjmAttempt[]>([])
  const [queue, setQueue] = useState<TjmReviewQueueItem[]>([])
  const [analytics, setAnalytics] = useState<TjmAnalytics | null>(null)
  const [questions, setQuestions] = useState<TjmQuestionVersion[]>([])
  const [attempt, setAttempt] = useState<TjmAttempt | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [commandLedger] = useState(
    () =>
      new TjmCommandLedger(
        undefined,
        sessionCommandStorage(),
        'deeptutor.tjm.workspaceCommands'
      )
  )

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const auth = await fetchAuthStatus()
      const admin = Boolean(auth?.is_admin || auth?.enabled === false)
      setIsAdmin(admin)
      const [nextExams, nextPreferences, nextHistory, nextQueue, nextAnalytics, nextQuestions] =
        await Promise.all([
          listTjmExams(),
          getTjmExamPreferences(),
          getTjmHistory(),
          getTjmReviewQueue(),
          getTjmAnalytics(),
          admin
            ? Promise.all([
                listTjmReviewQuestions('draft'),
                listTjmReviewQuestions('published'),
                listTjmReviewQuestions('retired'),
              ]).then(([drafts, published, retired]) =>
                selectAdminReviewQuestions(drafts, published, retired)
              )
            : Promise.resolve([]),
        ])
      setExams(nextExams)
      setPreferences(nextPreferences)
      setHistory(nextHistory)
      setQueue(nextQueue)
      setAnalytics(nextAnalytics)
      setQuestions(nextQuestions)
    } catch (reason) {
      setError(messageOf(reason))
    }
  }, [])

  useEffect(() => {
    let active = true
    const load = async () => {
      await refresh()
      const attemptId = window.sessionStorage.getItem(ACTIVE_ATTEMPT_KEY)
      if (attemptId && active) {
        try {
          const restored = await getTjmAttempt(attemptId)
          setAttempt(restored)
          if (restored.status !== 'in_progress') {
            window.sessionStorage.removeItem(ACTIVE_ATTEMPT_KEY)
          }
        } catch {
          window.sessionStorage.removeItem(ACTIVE_ATTEMPT_KEY)
        }
      }
      if (active) setLoading(false)
    }
    void load()
    return () => {
      active = false
    }
  }, [refresh])

  const openAttempt = (next: TjmAttempt) => {
    setAttempt(next)
    window.sessionStorage.setItem(ACTIVE_ATTEMPT_KEY, next.id)
  }

  const start = async (exam: TjmExam, mode: 'practice' | 'exam') => {
    setBusy(true)
    setError(null)
    const scope = `start:${exam.id}:${mode}`
    const command = commandLedger.begin(scope, () => ({ examId: exam.id, mode }))
    try {
      openAttempt(await startTjmAttempt(command.payload.examId, command.payload.mode, command.key))
      commandLedger.complete(scope, command.key)
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  const startReview = async (examId: string) => {
    setBusy(true)
    setError(null)
    const scope = `start-review:${examId}`
    const command = commandLedger.begin(scope, () => ({ examId, limit: 20 }))
    try {
      openAttempt(
        await startTjmReviewAttempt(command.payload.examId, command.payload.limit, command.key)
      )
      commandLedger.complete(scope, command.key)
      setTab('learn')
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  const savePracticeTarget = async (examId: string, target: number | null) => {
    setBusy(true)
    setError(null)
    try {
      const updated = await updateTjmExamPreference(examId, target)
      setPreferences(current => {
        const alreadyPresent = current.preferences.some(item => item.exam_id === examId)
        return {
          preferences: [
            ...current.preferences.filter(item => item.exam_id !== examId),
            updated,
          ],
          total: alreadyPresent ? current.total : current.total + 1,
        }
      })
    } finally {
      setBusy(false)
    }
  }

  const finalized = async (next: TjmAttempt) => {
    setAttempt(next)
    window.sessionStorage.removeItem(ACTIVE_ATTEMPT_KEY)
    await refresh()
  }

  const leaveAttempt = () => {
    if (attempt?.status !== 'in_progress') {
      window.sessionStorage.removeItem(ACTIVE_ATTEMPT_KEY)
    }
    setAttempt(null)
  }

  const activeExams = exams.filter(exam => exam.status === 'active')

  return (
    <main
      lang={TJM_LOCALE}
      className="h-full min-h-dvh overflow-y-auto bg-[var(--background)] text-[var(--foreground)]"
    >
      <div className="mx-auto w-full max-w-[1500px] px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
              <ListChecks size={14} /> {tjmText('workspace.eyebrow')}
            </div>
            <h1 className="font-serif text-[28px] font-semibold leading-tight tracking-[-0.02em] sm:text-[34px]">
              {tjmText('workspace.title')}
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">
              {tjmText('workspace.description')}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 text-[11px] text-[var(--muted-foreground)]">
            <ShieldCheck size={14} /> {tjmText('workspace.authorityBadge')}
          </div>
        </header>

        {!attempt ? (
          <nav
            className="mb-5 flex gap-3 overflow-x-auto border-b border-[var(--border)] sm:gap-5"
            aria-label={tjmText('aria.workspace')}
          >
            <TabButton
              active={tab === 'learn'}
              icon={BookCheck}
              label={tjmText('workspace.nav.learn')}
              onClick={() => setTab('learn')}
            />
            <TabButton
              active={tab === 'review'}
              icon={RotateCcw}
              label={tjmText('workspace.nav.review')}
              count={queue.length}
              onClick={() => setTab('review')}
            />
            <TabButton
              active={tab === 'insights'}
              icon={BarChart3}
              label={tjmText('workspace.nav.insights')}
              onClick={() => setTab('insights')}
            />
            {isAdmin ? (
              <TabButton
                active={tab === 'admin'}
                icon={ShieldCheck}
                label={tjmText('workspace.nav.admin')}
                count={questions.length}
                onClick={() => setTab('admin')}
              />
            ) : null}
          </nav>
        ) : null}

        {error ? (
          <div
            role="alert"
            className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--destructive)]/30 bg-[var(--destructive)]/5 px-4 py-3 text-sm text-[var(--destructive)]"
          >
            <span className="flex items-center gap-2">
              <CircleAlert size={16} />
              {error}
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              icon={<RefreshCw size={14} />}
              onClick={() => void refresh()}
            >
              {tjmText('common.retry')}
            </Button>
          </div>
        ) : null}

        {loading ? (
          <div className="flex min-h-[45vh] items-center justify-center gap-2 text-sm text-[var(--muted-foreground)]">
            <Loader2 size={18} className="animate-spin" /> {tjmText('workspace.loading')}
          </div>
        ) : attempt ? (
          <AttemptDesk
            attempt={attempt}
            busy={busy}
            onChange={setAttempt}
            onFinalize={next => void finalized(next)}
            onExit={leaveAttempt}
          />
        ) : tab === 'learn' ? (
          <ExamCards
            exams={activeExams}
            preferences={preferences.preferences}
            busy={busy}
            onStart={(exam, mode) => void start(exam, mode)}
            onUpdateTarget={savePracticeTarget}
          />
        ) : tab === 'review' ? (
          <ReviewPanel
            queue={queue}
            exams={exams}
            history={history}
            busy={busy}
            onStart={examId => void startReview(examId)}
          />
        ) : tab === 'insights' ? (
          <InsightsPanel analytics={analytics} />
        ) : isAdmin ? (
          <AdminPanel exams={exams} questions={questions} busy={busy} onRefresh={refresh} />
        ) : null}
      </div>
    </main>
  )
}
