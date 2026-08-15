import type { Metadata } from 'next'

import TjmWorkspace from '@/components/tjm/TjmWorkspace'
import jaTjm from '@/locales/ja/tjm.json'

export const metadata: Metadata = {
  title: `${jaTjm['workspace.title']} | DeepTutor`,
  description: jaTjm['workspace.description'],
}

export default function TjmPage() {
  return <TjmWorkspace />
}
