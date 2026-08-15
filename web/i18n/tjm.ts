import i18n from 'i18next'

import jaTjm from '@/locales/ja/tjm.json'
import { initI18n } from './init'

export const TJM_LOCALE = 'ja' as const
export type TjmTextKey = keyof typeof jaTjm
export type TjmTextValues = Record<string, string | number>

type TjmCodeGroup =
  | 'attempt.mode'
  | 'review.reason'
  | 'analytics.confidence'
  | 'admin.examStatus'
  | 'admin.importStatus'

const catalog = jaTjm as Record<string, string>

initI18n()
if (!i18n.hasResourceBundle(TJM_LOCALE, 'tjm')) {
  i18n.addResourceBundle(TJM_LOCALE, 'tjm', jaTjm, true, true)
}

export function hasTjmText(key: string): key is TjmTextKey {
  return Object.prototype.hasOwnProperty.call(catalog, key)
}

export function tjmText(key: TjmTextKey, values: TjmTextValues = {}): string {
  return String(
    i18n.t(key, {
      ...values,
      lng: TJM_LOCALE,
      ns: 'tjm',
    })
  )
}

export function tjmCodeText(group: TjmCodeGroup, value: string): string {
  const key = `${group}.${value}`
  return hasTjmText(key) ? tjmText(key) : tjmText('common.unknown')
}
