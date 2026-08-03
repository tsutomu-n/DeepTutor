'use client'

/* eslint-disable i18n/no-literal-ui-text -- TJM ships its detailed exam workspace copy as one English-first surface; the global route label remains in the existing en/zh catalogs. */

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
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { useTjmVoice } from '@/hooks/useTjmVoice'
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
  updateTjmDraft,
} from '@/lib/tjm-api'
import { TjmCommandLedger } from '@/lib/tjm-command'
import {
  hasGrade,
  type TjmAnalytics,
  type TjmAttempt,
  type TjmAttemptMode,
  type TjmExam,
  type TjmExamInput,
  type TjmImportBatch,
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
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`
}

function formatElapsed(milliseconds: number | null): string {
  if (milliseconds === null) return '—'
  const seconds = Math.round(milliseconds / 1000)
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

function percent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : 'An unexpected error occurred.'
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

function ExamCards({
  exams,
  busy,
  onStart,
}: {
  exams: TjmExam[]
  busy: boolean
  onStart: (exam: TjmExam, mode: 'practice' | 'exam') => void
}) {
  if (!exams.length) {
    return (
      <EmptyPanel icon={ScrollText} title="No active exam is available">
        An administrator must import, review, and publish enough questions before activating an
        exam. No sample answer key is bundled with TJM.
      </EmptyPanel>
    )
  }
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {exams.map(exam => (
        <article key={exam.id} className={`${panelClass} overflow-hidden`}>
          <div className="border-b border-[var(--border)] px-5 py-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="rounded-full border border-[var(--border)] bg-[var(--secondary)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
                Rev. {exam.revision}
              </span>
              <span className="text-xs text-[var(--muted-foreground)]">{exam.id}</span>
            </div>
            <h2 className="font-serif text-xl font-semibold tracking-tight text-[var(--foreground)]">
              {exam.title}
            </h2>
            <p className="mt-2 min-h-10 text-sm leading-5 text-[var(--muted-foreground)]">
              {exam.description || 'A deterministic multiple-choice assessment.'}
            </p>
          </div>
          <dl className="grid grid-cols-3 divide-x divide-[var(--border)] border-b border-[var(--border)] bg-[var(--secondary)]/45">
            <div className="px-4 py-3">
              <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Items
              </dt>
              <dd className="mt-1 font-mono text-sm font-semibold">{exam.question_count}</dd>
            </div>
            <div className="px-4 py-3">
              <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Time
              </dt>
              <dd className="mt-1 font-mono text-sm font-semibold">
                {formatDuration(exam.duration_seconds)}
              </dd>
            </div>
            <div className="px-4 py-3">
              <dt className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Pass
              </dt>
              <dd className="mt-1 font-mono text-sm font-semibold">{exam.pass_score ?? '—'}</dd>
            </div>
          </dl>
          <div className="flex flex-wrap gap-2 px-5 py-4">
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              icon={<Sparkles size={15} />}
              onClick={() => onStart(exam, 'practice')}
            >
              Practice
            </Button>
            <Button
              type="button"
              disabled={busy}
              icon={<TimerReset size={15} />}
              onClick={() => onStart(exam, 'exam')}
            >
              Timed exam
            </Button>
          </div>
        </article>
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
  const commandLedgerRef = useRef<TjmCommandLedger | null>(null)
  if (!commandLedgerRef.current) {
    commandLedgerRef.current = new TjmCommandLedger(
      undefined,
      sessionCommandStorage(),
      `deeptutor.tjm.attemptCommands.${attempt.id}`
    )
  }
  const commandLedger = commandLedgerRef.current
  const item = attempt.items[position]
  const finalized = attempt.status !== 'in_progress'
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
  const voice = useTjmVoice(async transcript => {
    const target = voiceTargetRef.current
    if (!target || target.attemptId !== attempt.id || target.position !== position) {
      throw new Error('The question changed before transcription finished. No answer was saved.')
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
      command.key
    )
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
          cancelCommand.key
        )
        commandLedger.complete(cancelScope, cancelCommand.key)
      } catch (reason) {
        setVoiceCandidate(candidate)
        throw new Error(
          `The speech did not map to a choice, and discarding it failed. Retry dismissal. ${messageOf(reason)}`
        )
      }
      throw new Error(
        `I heard “${candidate.transcript}”, but could not map it to one choice. Please try again or answer on screen.`
      )
    }
    setVoiceCandidate(candidate)
  })
  const { stopListening, stopSpeaking } = voice
  const interactionBusy =
    actionBusy || busy || voice.state !== 'idle' || voiceCandidate !== null || submitOpen
  const navigationLocked = !canNavigateTjmAttempt(attempt.status, serverOpened, interactionBusy)

  useEffect(() => {
    setPosition(current => Math.min(current, Math.max(0, attempt.items.length - 1)))
  }, [attempt.items.length])

  useEffect(() => {
    if (!(position in openedAtRef.current)) openedAtRef.current[position] = Date.now()
  }, [position])

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
              setError(`${messageOf(reason)} Refresh also failed: ${messageOf(refreshError)}`)
            }
            return
          }
        }
        setError(`Question timing could not start. ${messageOf(reason)}`)
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
    void stopListening().catch(reason => setError(messageOf(reason)))
  }, [position, stopListening])

  useEffect(() => {
    if (item?.grading_status === 'eligible') return
    voiceTargetRef.current = null
    setVoiceCandidate(null)
    void stopListening().catch(reason => setError(messageOf(reason)))
  }, [item?.grading_status, stopListening])

  useEffect(() => {
    if (!finalized) return
    commandLedger.clear()
    voiceTargetRef.current = null
    setVoiceCandidate(null)
    setSubmitOpen(false)
    void stopListening().catch(reason => setError(messageOf(reason)))
    stopSpeaking()
  }, [commandLedger, finalized, stopListening, stopSpeaking])

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
      try {
        await stopListening()
      } catch (reason) {
        if (active) {
          setError(
            `Microphone shutdown failed; deadline finalization continued. ${messageOf(reason)}`
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
          `The deadline was reached, but final status could not be refreshed. ${messageOf(reason)}`
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
      <EmptyPanel icon={CircleAlert} title="This attempt has no question items">
        Exit the attempt and ask an administrator to verify the active exam blueprint.
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
      setError(`${messageOf(reason)} Refresh also failed: ${messageOf(refreshError)}`)
    }
  }

  const toggleVoiceCapture = async () => {
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
      try {
        await voice.stopListening()
      } catch (reason) {
        setError(`Microphone shutdown failed; submission continued. ${messageOf(reason)}`)
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
    ...item.choices.map((choice, index) => `${index + 1}番。${choice.text}`),
  ].join('\n')

  return (
    <div className="grid min-h-[calc(100dvh-12rem)] gap-4 xl:grid-cols-[minmax(0,1fr)_270px]">
      <section className={`${panelClass} flex min-w-0 flex-col overflow-hidden`}>
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--secondary)]/35 px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="rounded-md border border-[var(--border)] bg-[var(--background)] px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider">
              {attempt.mode.replace('_', ' ')}
            </span>
            <span className="truncate text-sm font-medium">{attempt.exam_snapshot.title}</span>
          </div>
          <div className="flex items-center gap-4 font-mono text-xs tabular-nums">
            <span>
              {attempt.answered_count}/{attempt.items.length} answered
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
                Question {position + 1} · {item.area}
              </p>
              <h1 className="mt-3 max-w-3xl font-serif text-[21px] font-semibold leading-[1.55] tracking-[-0.01em] text-[var(--foreground)] sm:text-[24px]">
                {serverOpened ? item.stem : 'Starting server-side question timing…'}
              </h1>
            </div>
            {isConfirmed ? (
              <span className="flex shrink-0 items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                <Check size={13} /> Confirmed
              </span>
            ) : null}
          </div>

          <div className="mb-5 flex flex-wrap gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={!serverOpened || (deadlineReached && !finalized)}
              icon={voice.state === 'speaking' ? <VolumeX size={15} /> : <Volume2 size={15} />}
              onClick={() =>
                voice.state === 'speaking' ? voice.stopSpeaking() : void voice.speak(questionSpeech)
              }
            >
              {voice.state === 'speaking' ? 'Stop reading' : 'Read question'}
            </Button>
            {hasGrade(item) ? (
              <Button
                type="button"
                variant="ghost"
                icon={<Volume2 size={15} />}
                onClick={() =>
                  void voice.speak(
                    `${item.is_correct ? 'Correct.' : `Correct answer: ${item.correct_option_key}.`} ${item.explanation}`
                  )
                }
              >
                Read result
              </Button>
            ) : null}
          </div>

          <div className="grid gap-2.5" role="radiogroup" aria-label="Answer choices">
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
                  Confidence before seeing the result
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
                <span>Guessing</span>
                <span>Certain</span>
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
                <strong className="font-medium">Hint {index + 1}.</strong> {hint}
              </div>
            </div>
          ))}

          {hasGrade(item) ? (
            <div
              className={`mt-5 rounded-xl border p-4 ${item.is_correct ? 'border-emerald-500/25 bg-emerald-500/8' : 'border-[var(--destructive)]/25 bg-[var(--destructive)]/5'}`}
            >
              <p className="text-sm font-semibold">
                {item.is_correct ? 'Correct' : `Correct answer: ${item.correct_option_key}`}
              </p>
              <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
                {item.explanation || 'No explanation was supplied for this reviewed version.'}
              </p>
            </div>
          ) : null}

          {item.grading_status === 'content_invalidated' ? (
            <div
              role="status"
              className="mt-5 rounded-xl border border-amber-500/25 bg-amber-500/8 p-4 text-sm leading-6"
            >
              This question version was invalidated and is excluded from scoring and learning
              analytics. Its historical answer events remain in the audit record.
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
                  <Loader2 size={16} className="animate-spin" /> Starting server-side question
                  timing…
                </>
              ) : (
                <>
                  <span>Question timing is not active, so answers remain disabled.</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={() => setOpenRetry(value => value + 1)}
                  >
                    Retry
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
                Hint
              </Button>
            ) : null}
            {canAnswer ? (
              <Button
                type="button"
                variant="ghost"
                disabled={
                  actionBusy || busy || !['idle', 'listening', 'speech'].includes(voice.state)
                }
                loading={voice.state === 'loading' || voice.state === 'transcribing'}
                icon={<Mic size={15} />}
                onClick={() => void toggleVoiceCapture()}
              >
                {voice.state === 'speech'
                  ? 'Hearing speech…'
                  : voice.state === 'listening'
                    ? 'Stop microphone'
                    : voice.state === 'transcribing'
                      ? 'Transcribing…'
                      : 'Answer by voice'}
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
                {isConfirmed && attempt.mode === 'exam' ? 'Change answer' : 'Confirm answer'}
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
              Previous
            </Button>
            {position < attempt.items.length - 1 ? (
              <Button
                type="button"
                variant="secondary"
                disabled={navigationLocked}
                icon={<ChevronRight size={15} />}
                onClick={() => setPosition(value => value + 1)}
              >
                Next
              </Button>
            ) : finalized ? (
              <Button type="button" variant="secondary" onClick={onExit}>
                Back to exams
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                disabled={deadlineReached || navigationLocked}
                icon={<FileCheck2 size={15} />}
                onClick={() => setSubmitOpen(true)}
              >
                Submit {attempt.mode === 'exam' ? 'exam' : 'session'}
              </Button>
            )}
          </div>
        </footer>
      </section>

      <aside className={`${panelClass} h-fit overflow-hidden xl:sticky xl:top-4`}>
        {finalized ? (
          <div className="border-b border-[var(--border)] bg-[var(--foreground)] px-5 py-5 text-[var(--background)]">
            <p className="text-[10px] uppercase tracking-[0.16em] opacity-65">
              {attempt.content_invalidated_count > 0 ? 'Historical raw score' : 'Final result'}
            </p>
            <p className="mt-2 font-serif text-3xl font-semibold tabular-nums">
              {attempt.correct_count ?? 0}
              <span className="text-lg opacity-60">
                /{attempt.total_count ?? attempt.items.length}
              </span>
            </p>
            {attempt.content_invalidated_count > 0 ? (
              <p className="mt-2 text-xs leading-5 opacity-75">
                Content invalidated: {attempt.content_invalidated_count} question version
                {attempt.content_invalidated_count === 1 ? '' : 's'} no longer counts as an official
                learning result.
              </p>
            ) : null}
          </div>
        ) : null}
        <div className="p-4">
          <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.15em] text-[var(--muted-foreground)]">
            Answer sheet
          </p>
          <div className="grid grid-cols-5 gap-2">
            {attempt.items.map(entry => (
              <button
                key={entry.position}
                type="button"
                disabled={navigationLocked}
                onClick={() => setPosition(entry.position)}
                aria-label={`Question ${entry.position + 1}${entry.confirmed_option_key ? ', answered' : ''}`}
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
              <kbd className="font-mono">1–9</kbd> selects an option.
            </p>
            <p>
              <kbd className="font-mono">← →</kbd> moves between questions.
            </p>
          </div>
          {!finalized ? (
            <button
              type="button"
              disabled={navigationLocked}
              onClick={onExit}
              className="mt-4 text-xs text-[var(--muted-foreground)] underline-offset-4 hover:text-[var(--foreground)] hover:underline"
            >
              Leave and resume later
            </button>
          ) : null}
        </div>
      </aside>

      <ConfirmDialog
        open={voiceCandidate !== null}
        title={
          voiceCandidate?.proposed_option_key === null
            ? 'Voice answer not recognized'
            : 'Confirm voice answer'
        }
        confirmLabel={voiceCandidate?.proposed_option_key === null ? 'Dismiss' : 'Confirm answer'}
        busy={actionBusy}
        busyLabel="Saving…"
        onConfirm={() =>
          void (voiceCandidate?.proposed_option_key === null
            ? cancelVoiceAnswer()
            : confirmVoiceAnswer())
        }
        onCancel={() => void cancelVoiceAnswer()}
      >
        {voiceCandidate
          ? voiceCandidate.proposed_option_key === null
            ? `I heard “${voiceCandidate.transcript}”, but it did not map to one choice. Dismissal is retried with the same command key.`
            : `I heard “${voiceCandidate.transcript}”. Confirm option ${voiceCandidate.proposed_option_key}? The answer is saved only after this confirmation.`
          : ''}
      </ConfirmDialog>

      <ConfirmDialog
        open={submitOpen}
        title="Finalize this attempt?"
        confirmLabel="Finalize"
        busy={actionBusy}
        busyLabel="Grading…"
        onConfirm={() => void finalize()}
        onCancel={() => setSubmitOpen(false)}
      >
        {attempt.mode === 'exam'
          ? `${attempt.answered_count} of ${attempt.items.length} answers are confirmed. Official answers and explanations appear only after server-side submission.`
          : 'The server will grade confirmed answers deterministically and update your review queue.'}
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
          <h2 className="font-serif text-lg font-semibold">Review ledger</h2>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            Each reason remains explicit; one item may carry several review signals.
          </p>
        </div>
        {!queue.length ? (
          <div className="px-5 py-12 text-center text-sm text-[var(--muted-foreground)]">
            No review items are pending.
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
                      {items.length} question version{items.length === 1 ? '' : 's'}
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy}
                    icon={<RotateCcw size={14} />}
                    onClick={() => onStart(examId)}
                  >
                    Start review
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
                        <span className="font-mono text-[10px]">P{item.priority}</span>
                      </div>
                      <p className="mt-1.5 line-clamp-2 text-sm leading-5">{item.stem}</p>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {item.reasons.map(reason => (
                          <span
                            key={reason}
                            className="rounded-full bg-[var(--accent)] px-2 py-0.5 text-[10px]"
                          >
                            {reason.replaceAll('_', ' ')}
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
          <h2 className="font-serif text-lg font-semibold">Recent attempts</h2>
        </div>
        {!history.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">
            No attempt history yet.
          </p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {history.slice(0, 8).map(attempt => (
              <div key={attempt.id} className="flex items-center justify-between gap-4 px-5 py-3.5">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{attempt.exam_snapshot.title}</p>
                  <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]">
                    {attempt.mode} · {new Date(attempt.started_at).toLocaleDateString()}
                  </p>
                </div>
                <span className="font-mono text-sm font-semibold tabular-nums">
                  {attempt.correct_count ?? '—'}/{attempt.total_count ?? attempt.items.length}
                </span>
                {attempt.content_invalidated_count > 0 ? (
                  <span className="rounded-full bg-amber-500/10 px-2 py-1 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
                    Content invalidated · raw score retained
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
      <EmptyPanel icon={BarChart3} title="Analysis begins after the first submission">
        Accuracy, response time, confidence calibration, hint use, topic breakdowns, and attempt
        trends use finalized server history only.
      </EmptyPanel>
    )
  }
  const cards = [
    [
      'Accuracy',
      percent(analytics.overall.accuracy),
      `${analytics.overall.correct}/${analytics.overall.total} items`,
      Gauge,
    ],
    [
      'Response time',
      formatElapsed(analytics.overall.average_elapsed_ms),
      'average confirmed answer',
      Clock3,
    ],
    ['Hint use', percent(analytics.overall.hint_use_rate), 'of finalized items', Lightbulb],
    ['Attempts', String(analytics.trend.length), 'submitted or expired', History],
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
            <h2 className="font-serif text-lg font-semibold">Accuracy by area</h2>
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
            <h2 className="font-serif text-lg font-semibold">Confidence calibration</h2>
          </div>
          <div className="grid grid-cols-3 divide-x divide-[var(--border)] p-5">
            {Object.entries(analytics.confidence).map(([band, metric]) => (
              <div key={band} className="px-3 text-center first:pl-0 last:pr-0">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                  {band}
                </p>
                <p className="mt-2 font-serif text-2xl font-semibold tabular-nums">
                  {percent(metric.accuracy)}
                </p>
                <p className="mt-1 text-[10px] text-[var(--muted-foreground)]">
                  {metric.answered} answered
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
      aria-label="Edit draft question"
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
              {question.stable_id} · version {question.version}
            </p>
            <h2 className="mt-1 font-serif text-xl font-semibold">Edit draft</h2>
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
        <div className="grid gap-4">
          <label className="grid gap-1.5 text-xs font-medium">
            Question stem
            <textarea
              required
              value={stem}
              onChange={event => setStem(event.target.value)}
              rows={4}
              className={inputClass}
            />
          </label>
          <label className="grid gap-1.5 text-xs font-medium">
            Area
            <input
              required
              value={area}
              onChange={event => setArea(event.target.value)}
              className={inputClass}
            />
          </label>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-xs font-medium">Choices and official answer</legend>
            {choices.map((choice, index) => (
              <div key={choice.key} className="grid grid-cols-[auto_1fr] items-center gap-2">
                <label className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg border border-[var(--border)] font-mono text-xs">
                  <input
                    type="radio"
                    name="correct"
                    value={choice.key}
                    checked={correct === choice.key}
                    onChange={() => setCorrect(choice.key)}
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
            Explanation
            <textarea
              value={explanation}
              onChange={event => setExplanation(event.target.value)}
              rows={3}
              className={inputClass}
            />
          </label>
          <label className="grid gap-1.5 text-xs font-medium">
            Hints <span className="font-normal text-[var(--muted-foreground)]">One per line</span>
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
            Cancel
          </Button>
          <Button type="submit" loading={busy}>
            Save reviewed draft
          </Button>
        </div>
      </form>
    </div>
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
    durationMinutes: '120',
    questionCount: '50',
    passScore: '',
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
      setError('Blueprint must be a JSON object such as {"Civil law": 14}.')
      return
    }
    const input: TjmExamInput = {
      id: examForm.id,
      title: examForm.title,
      description: examForm.description,
      duration_seconds: Number(examForm.durationMinutes) * 60,
      question_count: Number(examForm.questionCount),
      pass_score: examForm.passScore ? Number(examForm.passScore) : null,
      blueprint,
    }
    await act(() => createTjmExam(input), 'Exam definition created as a draft.')
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
        `Import ${result.batch_id}: ${result.imported_rows} drafts created, ${result.duplicate_rows} duplicates skipped.`
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
            <h2 className="font-serif text-lg font-semibold">Exam definition</h2>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1 text-xs font-medium">
              ID
              <input
                required
                value={examForm.id}
                onChange={event => setExamForm({ ...examForm, id: event.target.value })}
                placeholder="takken-2026"
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              Title
              <input
                required
                value={examForm.title}
                onChange={event => setExamForm({ ...examForm, title: event.target.value })}
                placeholder="宅地建物取引士"
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium sm:col-span-2">
              Description
              <input
                value={examForm.description}
                onChange={event => setExamForm({ ...examForm, description: event.target.value })}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              Duration (minutes)
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
              Question count
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
              Pass score (optional)
              <input
                min={0}
                type="number"
                value={examForm.passScore}
                onChange={event => setExamForm({ ...examForm, passScore: event.target.value })}
                className={inputClass}
              />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              Blueprint JSON
              <input
                required
                value={examForm.blueprint}
                onChange={event => setExamForm({ ...examForm, blueprint: event.target.value })}
                className={`${inputClass} font-mono`}
              />
            </label>
          </div>
          <Button type="submit" className="mt-4" loading={localBusy || busy}>
            Create draft exam
          </Button>
        </form>

        <form onSubmit={importQuestions} className={`${panelClass} p-5`}>
          <div className="mb-4 flex items-center gap-2">
            <FileUp size={17} />
            <h2 className="font-serif text-lg font-semibold">Question import</h2>
          </div>
          <p className="mb-4 text-sm leading-6 text-[var(--muted-foreground)]">
            Upload strict UTF-8 JSON, JSONL, or CSV. Every row is validated before insertion and all
            imported questions remain drafts.
          </p>
          <label className="mb-3 grid gap-1 text-xs font-medium">
            Format
            <select name="format" className={inputClass} defaultValue="json">
              <option value="json">JSON array</option>
              <option value="jsonl">JSON Lines</option>
              <option value="csv">CSV</option>
            </select>
          </label>
          <label className="grid cursor-pointer gap-2 rounded-xl border border-dashed border-[var(--border)] bg-[var(--secondary)]/35 px-4 py-6 text-center text-sm hover:border-[var(--muted-foreground)]">
            <Upload size={20} className="mx-auto text-[var(--muted-foreground)]" />
            <span>Select question file</span>
            <input
              required
              name="file"
              type="file"
              accept=".json,.jsonl,.csv,application/json,text/csv"
              className="mx-auto max-w-full text-xs"
            />
          </label>
          <Button type="submit" className="mt-4" loading={localBusy || busy}>
            Validate and import
          </Button>
          {importResult ? (
            <p className="mt-3 font-mono text-[10px] text-[var(--muted-foreground)]">
              Batch {importResult.batch_id} · {importResult.status}
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
            <h2 className="font-serif text-lg font-semibold">Exam catalog</h2>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              Activation fails closed until the published blueprint is complete.
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            icon={<RefreshCw size={14} />}
            onClick={() => void onRefresh()}
          >
            Refresh
          </Button>
        </div>
        {!exams.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">No exam definitions.</p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {exams.map(exam => (
              <div
                key={exam.id}
                className="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5"
              >
                <div>
                  <p className="text-sm font-medium">{exam.title}</p>
                  <p className="text-[11px] text-[var(--muted-foreground)]">
                    {exam.id} · {exam.question_count} questions ·{' '}
                    {formatDuration(exam.duration_seconds)}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-[var(--secondary)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider">
                    {exam.status}
                  </span>
                  {exam.status === 'draft' ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={localBusy}
                      onClick={() =>
                        void act(() => activateTjmExam(exam.id), `${exam.title} is active.`)
                      }
                    >
                      Activate
                    </Button>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className={`${panelClass} overflow-hidden`}>
        <div className="border-b border-[var(--border)] px-5 py-4">
          <h2 className="font-serif text-lg font-semibold">Human review queue</h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            Edit, record a review decision, then publish. AI output never crosses this boundary
            automatically.
          </p>
        </div>
        {!questions.length ? (
          <p className="px-5 py-8 text-sm text-[var(--muted-foreground)]">
            No draft or legacy questions are waiting for review.
          </p>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {questions.map(question => (
              <article key={question.id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                      {question.exam_id} · {question.area} · {question.stable_id} v
                      {question.version}
                    </p>
                    <p className="mt-2 text-sm leading-6">{question.stem}</p>
                  </div>
                  <span
                    className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${question.reviewed_by ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'bg-amber-500/10 text-amber-700 dark:text-amber-300'}`}
                  >
                    {question.review_binding_state === 'legacy_unverified'
                      ? 'legacy re-review required'
                      : question.reviewed_by
                        ? 'reviewed'
                        : 'unreviewed'}
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
                      Edit
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
                          () => reviewTjmQuestion(question.id, 'Reviewed in TJM admin workspace'),
                          'Review decision recorded.'
                        )
                      }
                    >
                      Mark reviewed
                    </Button>
                  ) : adminReviewActions(question).canPublish ? (
                    <Button
                      type="button"
                      size="sm"
                      loading={localBusy}
                      onClick={() =>
                        void act(
                          () => publishTjmQuestion(question.id),
                          'Reviewed version published.'
                        )
                      }
                    >
                      Publish
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
                          () => rejectTjmQuestion(question.id, 'Rejected in TJM admin workspace'),
                          'Draft rejected.'
                        )
                      }
                    >
                      Reject
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
                          'Explain why this published question is invalid. This action preserves the audit history.'
                        )
                        if (!note?.trim()) return
                        void act(
                          () => retireTjmQuestion(question.id, note.trim()),
                          'Published content invalidated.'
                        )
                      }}
                    >
                      Invalidate content
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
                          'Replacement question version ID for this legacy retirement:'
                        )
                        if (!replacementId?.trim()) return
                        const note =
                          window.prompt('Audit note describing the historical evidence:') ?? ''
                        void act(
                          () =>
                            classifyLegacyTjmRetirement(
                              question.id,
                              replacementId.trim(),
                              note.trim()
                            ),
                          'Legacy retirement classified as superseded.'
                        )
                      }}
                    >
                      Classify superseded
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
            void act(() => updateTjmDraft(editing.id, input), 'Draft question updated.').then(() =>
              setEditing(null)
            )
          }
        />
      ) : null}
    </div>
  )
}

export default function TjmWorkspace() {
  const { t } = useTranslation()
  const [tab, setTab] = useState<WorkspaceTab>('learn')
  const [exams, setExams] = useState<TjmExam[]>([])
  const [history, setHistory] = useState<TjmAttempt[]>([])
  const [queue, setQueue] = useState<TjmReviewQueueItem[]>([])
  const [analytics, setAnalytics] = useState<TjmAnalytics | null>(null)
  const [questions, setQuestions] = useState<TjmQuestionVersion[]>([])
  const [attempt, setAttempt] = useState<TjmAttempt | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const commandLedgerRef = useRef<TjmCommandLedger | null>(null)
  if (!commandLedgerRef.current) {
    commandLedgerRef.current = new TjmCommandLedger(
      undefined,
      sessionCommandStorage(),
      'deeptutor.tjm.workspaceCommands'
    )
  }
  const commandLedger = commandLedgerRef.current

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const auth = await fetchAuthStatus()
      const admin = Boolean(auth?.is_admin || auth?.enabled === false)
      setIsAdmin(admin)
      const [nextExams, nextHistory, nextQueue, nextAnalytics, nextQuestions] = await Promise.all([
        listTjmExams(),
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
    <main className="h-full min-h-dvh overflow-y-auto bg-[var(--background)] text-[var(--foreground)]">
      <div className="mx-auto w-full max-w-[1500px] px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
              <ListChecks size={14} /> Test Judgment Module
            </div>
            <h1 className="font-serif text-[28px] font-semibold leading-tight tracking-[-0.02em] sm:text-[34px]">
              {t('TJM Exam Studio')}
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">
              Versioned questions, deliberate answers, deterministic grading, and an auditable
              review loop.
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 text-[11px] text-[var(--muted-foreground)]">
            <ShieldCheck size={14} /> Server-authoritative grading
          </div>
        </header>

        {!attempt ? (
          <nav
            className="mb-5 flex gap-3 overflow-x-auto border-b border-[var(--border)] sm:gap-5"
            aria-label="TJM workspace"
          >
            <TabButton
              active={tab === 'learn'}
              icon={BookCheck}
              label="Learn"
              onClick={() => setTab('learn')}
            />
            <TabButton
              active={tab === 'review'}
              icon={RotateCcw}
              label="Review"
              count={queue.length}
              onClick={() => setTab('review')}
            />
            <TabButton
              active={tab === 'insights'}
              icon={BarChart3}
              label="Insights"
              onClick={() => setTab('insights')}
            />
            {isAdmin ? (
              <TabButton
                active={tab === 'admin'}
                icon={ShieldCheck}
                label="Admin"
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
              Retry
            </Button>
          </div>
        ) : null}

        {loading ? (
          <div className="flex min-h-[45vh] items-center justify-center gap-2 text-sm text-[var(--muted-foreground)]">
            <Loader2 size={18} className="animate-spin" /> Loading TJM workspace…
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
            busy={busy}
            onStart={(exam, mode) => void start(exam, mode)}
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
