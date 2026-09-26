// Port of dashboard.py's render_person_drilldown.

import { useEffect, useState } from 'react'
import { getPersonHistory } from '../api'
import type { PersonHistoryRow, PlotlyFigureJSON } from '../types'
import Callout from './Callout'
import StatCard from './StatCard'
import ChartFigure from './ChartFigure'

interface PersonDrilldownProps {
  email: string | null
  name: string | null
  start: string
  end: string
  onClear: () => void
}

export default function PersonDrilldown({ email, name, start, end, onClear }: PersonDrilldownProps) {
  const [rows, setRows] = useState<PersonHistoryRow[] | null>(null)
  const [chart, setChart] = useState<PlotlyFigureJSON | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!email || !name) {
      setRows(null)
      setChart(null)
      setError(null)
      return
    }
    let cancelled = false
    setRows(null)
    setChart(null)
    setError(null)
    getPersonHistory(email, name, start, end)
      .then((res) => {
        if (cancelled) return
        setRows(res.rows)
        setChart(res.chart)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load person history.')
      })
    return () => {
      cancelled = true
    }
  }, [email, name, start, end])

  if (!email) {
    return <Callout variant="caption">Click a name in either table above to see their full stress history over time.</Callout>
  }

  const valid = (rows ?? []).filter((r) => r.avg_stress !== null && r.avg_stress !== undefined && !Number.isNaN(r.avg_stress))
  const deltas = (rows ?? []).map((r) => r.delta_stress).filter((d): d is number => d !== null && d !== undefined)

  return (
    <div className="person-drilldown">
      <hr />
      <div className="drilldown-header">
        <h3>📈 {name}'s stress history</h3>
        <button className="button button-secondary" onClick={onClear} title="Close this drill-down view">
          ✕ Clear
        </button>
      </div>

      {error && <Callout variant="error">{error}</Callout>}
      {!error && rows === null && <Callout variant="caption">Loading…</Callout>}
      {rows !== null && rows.length === 0 && (
        <Callout variant="info">No meeting history for {name} in this date range.</Callout>
      )}
      {rows !== null && rows.length > 0 && (
        <>
          <div className="stat-row">
            <StatCard label="Meetings" value={`${rows.length}`} />
            {valid.length > 0 && (
              <>
                <StatCard label="Avg stress" value={(valid.reduce((s, r) => s + r.avg_stress, 0) / valid.length).toFixed(0)} />
                <StatCard label="Peak stress" value={Math.max(...valid.map((r) => r.peak_stress)).toFixed(0)} />
              </>
            )}
            {deltas.length > 0 && (
              <StatCard
                label="Avg Δ (during vs. before)"
                value={`${deltas.reduce((s, d) => s + d, 0) / deltas.length >= 0 ? '+' : ''}${(deltas.reduce((s, d) => s + d, 0) / deltas.length).toFixed(1)}`}
              />
            )}
          </div>
          {chart && <ChartFigure figure={chart} />}
          <Callout variant="caption">
            One point per meeting occurrence with them, in chronological order &mdash; a recurring meeting on
            different days shows up as separate points, not flattened into a single average.
          </Callout>
        </>
      )}
    </div>
  )
}
