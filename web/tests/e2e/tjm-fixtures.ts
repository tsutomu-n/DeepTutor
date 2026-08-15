import type { APIRequestContext, APIResponse } from '@playwright/test'

export const SMALL_EXAM_ID = 'e2e-general-choice'
export const LARGE_EXAM_ID = 'e2e-general-choice-50'

interface SeededExam {
  id: string
  title: string
  questionCount: number
}

async function responseJson<T>(response: APIResponse, operation: string): Promise<T> {
  const text = await response.text()
  if (!response.ok()) {
    throw new Error(`${operation} failed (${response.status()}): ${text}`)
  }
  return JSON.parse(text) as T
}

function examPayload(exam: SeededExam) {
  return {
    id: exam.id,
    title: exam.title,
    description: '実在試験に依存しないE2E専用の合成データです。',
    duration_seconds: exam.questionCount === 50 ? 3000 : 180,
    question_count: exam.questionCount,
    official_passing_score: exam.questionCount === 50 ? 35 : 2,
    official_passing_score_source: {
      title: 'E2E合成データの判定基準',
      publisher: 'DeepTutor test fixture',
      published_at: '2026-08-03',
    },
    blueprint: { 共通分野: exam.questionCount },
  }
}

function questionPayload(exam: SeededExam) {
  return Array.from({ length: exam.questionCount }, (_, index) => ({
    exam_id: exam.id,
    stable_id: `${exam.id}-q-${String(index + 1).padStart(3, '0')}`,
    stem: `合成問題 ${index + 1}: 正しい番号を選んでください。`,
    options: [
      { key: '1', text: '誤りの合成選択肢' },
      { key: '2', text: '正しい合成選択肢' },
      { key: '3', text: '別の誤りの合成選択肢' },
      { key: '4', text: 'もう一つの誤りの合成選択肢' },
    ],
    correct_option_key: '2',
    area: '共通分野',
    explanation: 'この合成問題では2番だけを正解として定義しています。',
    hints: ['正解は合成fixtureの定義から決まります。'],
    source: { license: 'synthetic-test-fixture', generated: true },
  }))
}

async function createAndPublishExam(
  request: APIRequestContext,
  exam: SeededExam
): Promise<void> {
  await responseJson(
    await request.post('/api/v1/tjm/exams', { data: examPayload(exam) }),
    `create ${exam.id}`
  )

  await responseJson(
    await request.post('/api/v1/tjm/imports', {
      multipart: {
        import_format: 'json',
        file: {
          name: `${exam.id}.json`,
          mimeType: 'application/json',
          buffer: Buffer.from(JSON.stringify(questionPayload(exam))),
        },
      },
    }),
    `import ${exam.id}`
  )

  const drafts = await responseJson<{ questions: Array<{ id: string; exam_id: string }> }>(
    await request.get('/api/v1/tjm/review/questions?status=draft'),
    `list drafts for ${exam.id}`
  )
  const versionIds = drafts.questions
    .filter(question => question.exam_id === exam.id)
    .map(question => question.id)
  if (versionIds.length !== exam.questionCount) {
    throw new Error(
      `${exam.id} imported ${versionIds.length} draft questions; expected ${exam.questionCount}`
    )
  }
  for (const versionId of versionIds) {
    await responseJson(
      await request.post(`/api/v1/tjm/review/questions/${versionId}/review`, {
        data: { note: 'E2E合成データのレビュー' },
      }),
      `review ${versionId}`
    )
    await responseJson(
      await request.post(`/api/v1/tjm/review/questions/${versionId}/publish`),
      `publish ${versionId}`
    )
  }

  await responseJson(
    await request.post(`/api/v1/tjm/exams/${exam.id}/activate`),
    `activate ${exam.id}`
  )
}

export async function seedTjmFixtures(request: APIRequestContext): Promise<void> {
  await createAndPublishExam(request, {
    id: SMALL_EXAM_ID,
    title: '汎用択一ミニ試験',
    questionCount: 3,
  })
  await createAndPublishExam(request, {
    id: LARGE_EXAM_ID,
    title: '汎用択一50問試験',
    questionCount: 50,
  })
  await responseJson(
    await request.put(`/api/v1/tjm/exam-preferences/${SMALL_EXAM_ID}`, {
      data: { practice_target_score: 2 },
    }),
    'set personal practice target'
  )
}

export async function ensureTjmFixtures(request: APIRequestContext): Promise<void> {
  const response = await request.get('/api/v1/tjm/exams')
  const body = await responseJson<{ exams: Array<{ id: string }> }>(response, 'inspect E2E exams')
  const ids = new Set(body.exams.map(exam => exam.id))
  if (ids.has(SMALL_EXAM_ID) && ids.has(LARGE_EXAM_ID)) return
  if (ids.has(SMALL_EXAM_ID) || ids.has(LARGE_EXAM_ID)) {
    throw new Error('TJM E2E fixture is only partially initialized')
  }
  await seedTjmFixtures(request)
}
