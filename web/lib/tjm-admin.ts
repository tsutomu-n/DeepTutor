import type { TjmQuestionVersion } from '@/lib/tjm-types'

export function selectAdminReviewQuestions(
  drafts: TjmQuestionVersion[],
  published: TjmQuestionVersion[]
): TjmQuestionVersion[] {
  return [
    ...drafts,
    ...published.filter(question => question.review_binding_state === 'legacy_unverified'),
  ]
}

export function adminReviewActions(question: TjmQuestionVersion) {
  const draft = question.status === 'draft'
  const legacyPublication =
    question.status === 'published' && question.review_binding_state === 'legacy_unverified'
  return {
    canEdit: draft,
    canReview: (draft || legacyPublication) && question.review_binding_state !== 'current',
    canPublish: draft && question.review_binding_state === 'current',
    canReject: draft,
  }
}
