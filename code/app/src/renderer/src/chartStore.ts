/** The ChartStore adapter: ChartDraw's persistence hook, wired to the API.
 *
 *  This file exists so ChartDraw itself imports nothing but lightweight-charts
 *  types. The engine's arithmetic is gate-tested by importing it directly under
 *  plain node (selftest `_chart_time`), which an `import { api }` — and the
 *  `window.grindstone` bridge behind it — would break.
 */
import { api, ApiError } from './api'
import type { ChartDoc, ChartStore } from './components/ChartDraw'

interface DocResponse {
  key: string
  doc: ChartDoc
}

/** `onError` receives a message when a save fails and null when one succeeds,
 *  so a page can show — and then clear — "drawings not saved: …". Losing work
 *  silently is the one outcome worth spending UI on. */
export function makeChartStore(onError?: (msg: string | null) => void): ChartStore {
  return {
    async load(key: string): Promise<ChartDoc | null> {
      const res = await api<DocResponse>(
        'GET',
        `/api/chart-objects?key=${encodeURIComponent(key)}`
      )
      return res?.doc ?? null
    },

    async save(key: string, doc: ChartDoc): Promise<void> {
      try {
        await api<DocResponse>('PUT', '/api/chart-objects', { key, doc })
        onError?.(null)
      } catch (e) {
        // A 422 is this app's own bug — the engine produced a document its
        // backend refuses — so it must not read as a network blip. Either way
        // the engine keeps the objects in memory and retries on the next edit.
        const detail = e instanceof ApiError ? e.message : String(e)
        onError?.(detail)
        throw e
      }
    },
  }
}
