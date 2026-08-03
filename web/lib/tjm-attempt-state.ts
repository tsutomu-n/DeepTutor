import type { TjmAttemptMode, TjmAttemptStatus } from '@/lib/tjm-types'

interface TjmAttemptInteractionInput {
  status: TjmAttemptStatus
  mode: TjmAttemptMode
  confirmed: boolean
  serverOpened: boolean
  secondsLeft: number | null
}

export function canAnswerTjmItem(input: TjmAttemptInteractionInput): boolean {
  if (input.status !== 'in_progress' || !input.serverOpened || input.secondsLeft === 0) {
    return false
  }
  return !input.confirmed || input.mode === 'exam'
}

export function shouldRefreshExpiredTjmAttempt(
  status: TjmAttemptStatus,
  mode: TjmAttemptMode,
  secondsLeft: number | null
): boolean {
  return status === 'in_progress' && mode === 'exam' && secondsLeft === 0
}
