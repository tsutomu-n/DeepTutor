import { expect, test } from '@playwright/test'

import { ensureTjmFixtures } from './tjm-fixtures'

test.beforeAll(async ({ request }) => {
  await ensureTjmFixtures(request)
})

test('狭幅表示でも50問試験を画面操作できる', async ({ page }) => {
  await page.goto('/tjm')
  const main = page.locator('main[lang="ja"]')
  await expect(main).toBeVisible()
  await expect(page.getByRole('heading', { level: 1, name: 'TJM 試験学習' })).toBeVisible()

  const exam = page.locator('article').filter({ hasText: '汎用択一50問試験' }).first()
  await exam.getByRole('button', { name: '試験モード', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1, name: /合成問題 \d+:/ })).toBeVisible()
  await expect(page.getByRole('radio')).toHaveCount(4)
  await expect(page.getByRole('button', { name: /^第\d+問(?:、回答済み)?$/ })).toHaveCount(50)

  const overflow = await main.evaluate(element => element.scrollWidth - element.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await page.getByRole('radio').nth(1).click()
  await page.getByRole('button', { name: '回答を確定', exact: true }).click()
  await expect(page.getByText('確定済み', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '次の問題', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1, name: /合成問題 \d+:/ })).toBeVisible()
})
