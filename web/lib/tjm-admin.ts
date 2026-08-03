import type { TjmQuestionVersion } from '@/lib/tjm-types'

export function selectAdminReviewQuestions(
  drafts: TjmQuestionVersion[],
  published: TjmQuestionVersion[],
  retired: TjmQuestionVersion[] = []
): TjmQuestionVersion[] {
  return [
    ...drafts,
    ...published,
    ...retired.filter(question => question.retirement_reason === null),
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
    canRetire: question.status === 'published',
    canClassifySuperseded: question.status === 'retired' && question.retirement_reason === null,
  }
}
