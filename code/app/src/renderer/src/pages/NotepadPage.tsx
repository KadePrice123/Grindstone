/**
 * The notepad (docs/DATA_EXCHANGE.md §3): everything Get data has grabbed,
 * rendered by KIND, editable, removable.
 *
 * RENDERING IS PART OF ENROLLMENT (DX-1.4), not decoration on top of it. A
 * kind that cannot display itself properly is not enrolled — which is what
 * keeps this page from ever degrading into a wall of raw JSON. Every renderer
 * below corresponds to exactly one entry in the v1 enrollment table, and the
 * gate asserts the two lists match.
 *
 * Deep editing is deliberately NOT reinvented here: the editor for chart data
 * is a chart. Post it to a chart, edit it there, grab it back. This page
 * owns the edits that have no better home — a note's text, an entry's label,
 * removing rows from a chain — and every one of them REVALIDATES on the
 * backend, because an edited payload must still be the typed thing its kind
 * claims or every post target downstream breaks.
 */
import { useCallback, useEffect, useState, type ReactElement } from 'react'
import { api, ApiError } from '../api'
import { DataIcon } from '../components/icons'
import type { DataPayload, PadEntry } from '../datapad'

function fmt(n: unknown, dp = 2): string {
  return typeof n === 'number' && Number.isFinite(n) ? n.toFixed(dp) : '—'
}

function when(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

/** THE 13 FIELDS, in the backend's own order. Named here rather than derived
 *  from the row so a payload that lost a column shows a blank cell instead of
 *  silently narrowing the table — the drop would otherwise be invisible. */
const CHAIN_COLS: Array<[string, string]> = [
  ['strike', 'strike'], ['expiration', 'exp'], ['right', 'C/P'],
  ['bid', 'bid'], ['ask', 'ask'], ['last', 'last'],
  ['iv', 'iv'], ['delta', 'Δ'], ['gamma', 'Γ'],
  ['theta', 'Θ'], ['vega', 'V'], ['rho', 'ρ'],
]

function ChainTable({ rows }: { rows: Array<Record<string, unknown>> }): ReactElement {
  const [all, setAll] = useState(false)
  const shown = all ? rows : rows.slice(0, 8)
  return (
    <>
      <div className="np-scroll">
        <table className="np-table">
          <thead>
            <tr>
              {CHAIN_COLS.map(([k, label]) => <th key={k}>{label}</th>)}
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={String(r.occ_symbol ?? i)}>
                {CHAIN_COLS.map(([k]) => (
                  <td key={k} className={k === 'strike' ? 'em' : undefined}>
                    {k === 'expiration'
                      ? String(r[k] ?? '—').slice(5)
                      : k === 'right'
                        ? String(r[k] ?? '—')
                        : k === 'iv'
                          ? (typeof r[k] === 'number' ? `${((r[k] as number) * 100).toFixed(0)}%` : '—')
                          : fmt(r[k], k === 'strike' ? 1 : 2)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 8 ? (
        <button className="btn-link" onClick={() => setAll((v) => !v)}>
          {all ? 'show fewer' : `show all ${rows.length} contracts`}
        </button>
      ) : null}
    </>
  )
}

function Rows({ pairs }: { pairs: Array<[string, unknown]> }): ReactElement {
  return (
    <div className="np-rows">
      {pairs.map(([k, v]) => (
        <div className="np-row" key={k}>
          <span className="dim">{k}</span>
          <span>{typeof v === 'object' ? JSON.stringify(v) : String(v ?? '—')}</span>
        </div>
      ))}
    </div>
  )
}

/** One payload, by kind. Every branch here is an enrollment's declared
 *  renderer; the fallthrough exists only so an entry written by a NEWER
 *  build than this renderer degrades to something honest instead of blank. */
function Body({ payload }: { payload: DataPayload }): ReactElement {
  const d = payload.data as Record<string, unknown>
  switch (payload.kind) {
    case 'note':
      return <div className="np-note">{String(d.text ?? '')}</div>
    case 'chain':
      return <ChainTable rows={(d.contracts as Array<Record<string, unknown>>) ?? []} />
    case 'contract':
      return (
        <Rows pairs={[
          ['contract', d.occ_symbol],
          ['strike', fmt(d.strike, 1)],
          ['expiration', d.expiration],
          ['side', d.right === 'C' ? 'call' : 'put'],
          ['bid / ask', `${fmt(d.bid)} × ${fmt(d.ask)}`],
          ['Δ / iv', `${fmt(d.delta)} / ${typeof d.iv === 'number' ? `${(d.iv * 100).toFixed(0)}%` : '—'}`],
        ]} />
      )
    case 'chart-doc': {
      const doc = (d.doc ?? {}) as Record<string, unknown[]>
      return (
        <Rows pairs={[
          ['chart', d.key],
          ['drawings', (doc.drawings ?? []).length],
          ['legs', (doc.legs ?? []).length],
          ['constraints', (doc.constraints ?? []).length],
          ['measures', (doc.measures ?? []).length],
        ]} />
      )
    }
    case 'drawing': {
      const sub = (d.subdoc ?? {}) as Record<string, unknown[]>
      const n = (sub.drawings ?? []).length
      return (
        <Rows pairs={[
          // The component, spelled out: this is the whole point of the grab
          // rule — a constrained line brings its chain of objects with it.
          ['lines', n],
          ['legs', (sub.legs ?? []).length],
          ['constraints', (sub.constraints ?? []).length],
          ['measures', (sub.measures ?? []).length],
          ['linked objects', Math.max(0, n - 1) + (sub.legs ?? []).length],
        ]} />
      )
    }
    case 'backtest-spec': {
      const spec = (d.spec ?? d) as Record<string, unknown>
      const legs = (spec.legs as Array<Record<string, unknown>>) ?? []
      return (
        <Rows pairs={[
          ['name', spec.name ?? d.name ?? '—'],
          ['capital', spec.capital ?? '—'],
          ['legs', legs.map((l) =>
            `${l.action} ${l.right} ${l.delta !== undefined ? `Δ${l.delta}` : l.strike}@${l.dte}d`
          ).join(', ') || '—'],
        ]} />
      )
    }
    case 'form':
      return <Rows pairs={Object.entries((d.values as Record<string, unknown>) ?? {})} />
    case 'leg':
      return <Rows pairs={Object.entries((d.resolved as Record<string, unknown>) ?? {})} />
    default:
      return (
        <div className="dim subtle">
          this entry is a {payload.kind}, which this build has no renderer for —
          a newer version of the app wrote it
        </div>
      )
  }
}

export function NotepadPage(): ReactElement {
  const [entries, setEntries] = useState<PadEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const refresh = useCallback(async () => {
    try {
      setEntries(await api<PadEntry[]>('GET', '/api/notepad'))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const h = (): void => void refresh()
    // A grab from anywhere lands here immediately — the page is the truth
    // about what you hold, so it must not go stale while open.
    window.addEventListener('datapad:announce', h)
    return () => window.removeEventListener('datapad:announce', h)
  }, [refresh])

  const remove = async (id: string): Promise<void> => {
    try {
      await api('DELETE', `/api/notepad/${id}`)
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const saveEdit = async (entry: PadEntry): Promise<void> => {
    try {
      const body: Record<string, unknown> =
        entry.payload.kind === 'note'
          ? { payload: { ...entry.payload, data: { text: draft } } }
          : { label: draft }
      await api('PATCH', `/api/notepad/${entry.id}`, body)
      setEditing(null)
      await refresh()
    } catch (e) {
      // The backend revalidates every edit; its refusal is the message.
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const addNote = async (): Promise<void> => {
    try {
      await api('POST', '/api/notepad', {
        payload: {
          v: 1, kind: 'note', data: { text: 'new note' },
          provenance: {
            workspace: 'user', capturedAt: new Date().toISOString(), page: 'notepad',
          },
        },
      })
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <div className="page" data-wheel-context="notepad">
      <div className="page-head">
        <DataIcon />
        <h1>Notepad</h1>
        {entries ? <span className="dim">{entries.length} held</span> : null}
        <button className="btn" onClick={addNote}>Add a note</button>
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      {entries === null ? (
        <div className="card dim">Loading…</div>
      ) : entries.length === 0 ? (
        <div className="card dim">
          Nothing held yet. Right-click a chart, a chain row or a heatmap cell
          and pick <strong>Get data</strong>.
        </div>
      ) : (
        entries.map((e) => (
          <div className="card np-entry" key={e.id} data-entry-kind={e.payload.kind}>
            <div className="np-head">
              <span className="badge data">{e.payload.kind}</span>
              <strong>{e.label}</strong>
              <span className="subtle">
                {e.payload.provenance.workspace === 'agent' ? 'from the AI · ' : ''}
                {when(e.added_at)}
              </span>
              {/* Provenance is ROUTABLE (DX-7d): the address the grab recorded
                  reopens its source, which is the whole point of tracking it. */}
              {e.payload.provenance.address ? (
                <button
                  className="btn-link"
                  onClick={() => window.grindstone.openTab(
                    `symbol:${(e.payload.provenance.symbol ?? '').toUpperCase()}`
                  )}
                  disabled={!e.payload.provenance.symbol}
                  title={e.payload.provenance.address}
                >
                  open source
                </button>
              ) : null}
              <button
                className="btn-link"
                onClick={() => {
                  setEditing(e.id)
                  setDraft(e.payload.kind === 'note'
                    ? String((e.payload.data as { text?: string }).text ?? '')
                    : e.label)
                }}
              >
                {e.payload.kind === 'note' ? 'edit' : 'rename'}
              </button>
              <button className="btn-link bad" onClick={() => remove(e.id)}>remove</button>
            </div>

            {editing === e.id ? (
              <div className="np-edit">
                <textarea
                  className="field"
                  rows={e.payload.kind === 'note' ? 4 : 1}
                  value={draft}
                  onChange={(ev) => setDraft(ev.target.value)}
                />
                <button className="btn" onClick={() => saveEdit(e)}>Save</button>
                <button className="btn-link" onClick={() => setEditing(null)}>Cancel</button>
              </div>
            ) : (
              <Body payload={e.payload} />
            )}
          </div>
        ))
      )}
    </div>
  )
}
