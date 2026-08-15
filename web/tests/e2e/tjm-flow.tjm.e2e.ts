import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

import { ensureTjmFixtures, LARGE_EXAM_ID, SMALL_EXAM_ID } from './tjm-fixtures'

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request }) => {
  await ensureTjmFixtures(request)
})

async function openWorkspace(page: Page): Promise<void> {
  await page.goto('/tjm')
  await expect(page.locator('main[lang="ja"]')).toBeVisible()
  await expect(page.getByRole('heading', { level: 1, name: 'TJM 試験学習' })).toBeVisible()
  await expect(page.getByText('汎用択一ミニ試験')).toBeVisible()
}

function examCard(page: Page, title: string) {
  return page.locator('article').filter({ hasText: title }).first()
}

async function waitForOpenedQuestion(page: Page): Promise<void> {
  await expect(page.getByRole('heading', { level: 1, name: /合成問題 \d+:/ })).toBeVisible()
  await expect(page.getByRole('radio')).toHaveCount(4)
  await expect(page.getByRole('radio').first()).toBeEnabled()
}

async function confirmChoice(page: Page, choiceIndex: number): Promise<void> {
  await page.getByRole('radio').nth(choiceIndex).click()
  await page.getByRole('button', { name: '回答を確定', exact: true }).click()
  await expect(page.getByText('確定済み', { exact: true })).toBeVisible()
}

test('実APIと一時SQLiteへ3問・50問の汎用試験を構築できる', async ({ request }) => {
  const response = await request.get('/api/v1/tjm/exams')
  const text = await response.text()
  expect(response.ok(), text).toBeTruthy()
  const body = JSON.parse(text) as {
    exams: Array<{ id: string; question_count: number; status: string }>
  }
  expect(body.exams).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ id: SMALL_EXAM_ID, question_count: 3, status: 'active' }),
      expect.objectContaining({ id: LARGE_EXAM_ID, question_count: 50, status: 'active' }),
    ])
  )
})

test('主要画面と管理境界を固定日本語で表示する', async ({ page }) => {
  await openWorkspace(page)

  const main = page.locator('main[lang="ja"]')
  await expect(page.getByRole('navigation', { name: 'TJM学習画面' })).toBeVisible()
  for (const label of ['学習', '復習', '分析', '管理']) {
    await expect(page.getByRole('button', { name: new RegExp(`^${label}`) })).toBeVisible()
  }
  const text = await main.innerText()
  expect(text).not.toMatch(/\b(?:Learn|Review|Insights|Admin|Timed exam)\b/)

  await page.getByRole('button', { name: /^管理/ }).click()
  for (const heading of ['試験定義', '問題の取り込み', '試験一覧', '人間レビュー待ち']) {
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible()
  }
  await expect(page.getByText('汎用択一50問試験')).toBeVisible()
})

test('通常演習からヒント・自信度・復習・履歴分析まで記録する', async ({ page }) => {
  await openWorkspace(page)
  await examCard(page, '汎用択一ミニ試験')
    .getByRole('button', { name: '通常演習', exact: true })
    .click()
  await waitForOpenedQuestion(page)

  await page.getByRole('slider', { name: '結果を見る前の自信度' }).fill('80')
  await page.getByRole('button', { name: 'ヒントを見る', exact: true }).click()
  await expect(page.getByText('ヒント1。', { exact: true })).toBeVisible()
  await confirmChoice(page, 1)
  await expect(page.getByText('正解', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '次の問題', exact: true }).click()
  await waitForOpenedQuestion(page)
  await page.getByRole('slider', { name: '結果を見る前の自信度' }).fill('20')
  await confirmChoice(page, 0)
  await expect(page.getByText('正解：2', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '次の問題', exact: true }).click()
  await waitForOpenedQuestion(page)
  await confirmChoice(page, 1)
  const submitTrigger = page.getByRole('button', { name: '演習を提出', exact: true })
  await submitTrigger.focus()
  await expect(submitTrigger).toBeFocused()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('alertdialog', { name: 'この回答回を確定しますか？' })
  await expect(dialog).toContainText('サーバーが確定済み回答を決定論的に採点')
  const cancel = dialog.getByRole('button', { name: 'キャンセル', exact: true })
  const close = dialog.getByRole('button', { name: '閉じる', exact: true })
  const confirm = dialog.getByRole('button', { name: '確定する', exact: true })
  await expect(cancel).toBeFocused()
  expect(
    await dialog.evaluate(node => ({
      label: document.getElementById(node.getAttribute('aria-labelledby') ?? '')?.textContent,
      description: document.getElementById(node.getAttribute('aria-describedby') ?? '')?.textContent,
    }))
  ).toEqual({
    label: 'この回答回を確定しますか？',
    description: expect.stringContaining('サーバーが確定済み回答を決定論的に採点'),
  })
  await page.keyboard.press('Shift+Tab')
  await expect(close).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(confirm).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(submitTrigger).toBeFocused()

  await submitTrigger.click()
  await expect(dialog.getByRole('button', { name: 'キャンセル', exact: true })).toBeFocused()
  await dialog.getByRole('button', { name: '確定する', exact: true }).click()

  await expect(page.getByText('最終結果', { exact: true })).toBeVisible()
  await expect(page.getByText('2/3', { exact: true })).toBeVisible()
  await expect(page.getByText('目標達成', { exact: true })).toBeVisible()
  await expect(page.getByText('この回答モードは、この判定の対象外です。')).toBeVisible()

  await page.getByRole('button', { name: '試験一覧へ戻る', exact: true }).click()
  await page.getByRole('button', { name: /復習/ }).click()
  await expect(page.getByRole('heading', { name: '復習台帳', exact: true })).toBeVisible()
  await expect(page.getByText('不正解', { exact: true })).toBeVisible()
  await expect(page.getByText('ヒントを使用', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '最近の回答履歴', exact: true })).toBeVisible()
  await expect(page.getByText('2/3', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '分析', exact: true }).click()
  await expect(page.getByText('67%', { exact: true })).toBeVisible()
  await expect(page.getByText('正答率', { exact: true })).toBeVisible()
  await expect(page.getByText('ヒント使用率', { exact: true })).toBeVisible()
})

test('試験モードは提出前の正解を隠し、再読込後も回答を保って決定論的に採点する', async ({ page }) => {
  await openWorkspace(page)
  await examCard(page, '汎用択一ミニ試験')
    .getByRole('button', { name: '試験モード', exact: true })
    .click()
  await waitForOpenedQuestion(page)

  await confirmChoice(page, 1)
  await expect(page.getByText('この合成問題では2番だけを正解として定義しています。')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '結果を読み上げる', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '第1問、回答済み' })).toBeVisible()

  await page.reload()
  await waitForOpenedQuestion(page)
  await expect(page.getByText('確定済み', { exact: true })).toBeVisible()
  await expect(page.getByText('この合成問題では2番だけを正解として定義しています。')).toHaveCount(0)

  await page.getByRole('button', { name: '次の問題', exact: true }).click()
  await waitForOpenedQuestion(page)
  await confirmChoice(page, 1)
  await page.getByRole('button', { name: '次の問題', exact: true }).click()
  await waitForOpenedQuestion(page)
  await confirmChoice(page, 1)

  await page.getByRole('button', { name: '試験を提出', exact: true }).click()
  const dialog = page.getByRole('alertdialog', { name: 'この回答回を確定しますか？' })
  await expect(dialog).toContainText('提出処理が完了した後にのみ表示')
  await dialog.getByRole('button', { name: '確定する', exact: true }).click()

  await expect(page.getByText('3/3', { exact: true })).toBeVisible()
  await expect(page.getByText('合格基準以上', { exact: true })).toBeVisible()
  await expect(page.getByText('目標達成', { exact: true })).toBeVisible()
  await expect(page.getByText('この合成問題では2番だけを正解として定義しています。')).toBeVisible()
  await expect(page.getByRole('button', { name: '結果を読み上げる', exact: true })).toBeVisible()
})

test('50問の試験を開始して問題順を再読込後も維持する', async ({ page, request }) => {
  await openWorkspace(page)
  await examCard(page, '汎用択一50問試験')
    .getByRole('button', { name: '試験モード', exact: true })
    .click()
  await waitForOpenedQuestion(page)
  await expect(page.getByRole('button', { name: /^第\d+問(?:、回答済み)?$/ })).toHaveCount(50)
  const firstStem = await page.getByRole('heading', { level: 1, name: /合成問題 \d+:/ }).innerText()
  const attemptId = await page.evaluate(() =>
    window.sessionStorage.getItem('deeptutor.tjm.activeAttemptId')
  )
  expect(attemptId).toBeTruthy()
  const beforeReload = await request.get(`/api/v1/tjm/attempts/${attemptId}`)
  expect(beforeReload.ok(), await beforeReload.text()).toBeTruthy()
  const beforeOrder = ((await beforeReload.json()) as {
    items: Array<{ question_version_id: string }>
  }).items.map(item => item.question_version_id)
  expect(beforeOrder).toHaveLength(50)
  expect(new Set(beforeOrder).size).toBe(50)

  await page.reload()
  await waitForOpenedQuestion(page)
  await expect(page.getByRole('heading', { level: 1, name: firstStem, exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /^第\d+問(?:、回答済み)?$/ })).toHaveCount(50)
  const afterReload = await request.get(`/api/v1/tjm/attempts/${attemptId}`)
  expect(afterReload.ok(), await afterReload.text()).toBeTruthy()
  const afterOrder = ((await afterReload.json()) as {
    items: Array<{ question_version_id: string }>
  }).items.map(item => item.question_version_id)
  expect(afterOrder).toEqual(beforeOrder)

  const finalized = await request.post(`/api/v1/tjm/attempts/${attemptId}/submit`, {
    headers: { 'Idempotency-Key': 'e2e-finalize-large-desktop' },
  })
  expect(finalized.ok(), await finalized.text()).toBeTruthy()
})
