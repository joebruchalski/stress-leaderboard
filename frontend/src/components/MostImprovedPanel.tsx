// Port of dashboard.py's render_most_improved_panel.

import { useEffect, useState } from 'react'
import { getMostImproved } from '../api'
import type { MostImprovedRow } from '../types'
import { divergingBarStyle, fmt0, fmt1, fmtSigned1, greenTintInverted } from '../severity'
import Callout from './Callout'

interface MostImprovedPanelProps {
  start: string
  end: string
  minMeetings: number
  onSelect: (email: string, name: string) => void
}

export default function MostImprovedPanel({ start, end, minMeetings, onSelect }: MostImprovedPanelProps) {
  const [rows, setRows] = useState<MostImprovedRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setRows(null)
    setError(null)
    getMostImproved(start, end)
      .then((res) => {
        if (!cancelled) setRows(res.rows)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load most-improved data.')
      })
    return () => {
      cancelled = true
    }
  }, [start, end])

  return (
    <section className="most-improved-panel">
      <h3>🕊️ Most Improved / Least Stressful</h3>
      {error && <Callout variant="error">{error}</Callout>}
      {!error && rows === null && <Callout variant="caption">Loading…</Callout>}
      {rows !== null && rows.length === 0 && <Callout variant="caption">No attendee data in this range yet.</Callout>}
      {rows !== null && rows.length > 0 && <MostImprovedTable rows={rows} minMeetings={minMeetings} onSelect={onSelect} />}
    </section>
  )
}

function MostImprovedTable({
  rows,
  minMeetings,
  onSelect,
}: {
  rows: MostImprovedRow[]
  minMeetings: number
  onSelect: (email: string, name: string) => void
}) {
  const filtered = rows.filter((r) => r.meetings >= minMeetings)
  if (filtered.length === 0) {
    return <Callout variant="caption">No one meets the minimum-meetings threshold above.</Callout>
  }

  const haveTrend = filtered.filter((r) => r.trend !== null).sort((a, b) => (a.trend as number) - (b.trend as number))
  const noTrend = filtered.filter((r) => r.trend === null).sort((a, b) => a.avg_stress - b.avg_stress)
  const ranked = [...haveTrend, ...noTrend].slice(0, 10)

  return (
    <>
      <table className="data-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Attendee</th>
            <th>Meetings</th>
            <th>Avg stress</th>
            <th>Trend (2nd half vs. 1st half)</th>
          </tr>
        </thead>
        <tbody>
          {ranked.map((row, i) => (
            <tr key={row.attendee_email} className="clickable-row" onClick={() => onSelect(row.attendee_email, row.attendee_name)}>
              <td>{i + 1}</td>
              <td>{row.attendee_name}</td>
              <td>{fmt0(row.meetings)}</td>
              <td style={{ background: greenTintInverted(row.avg_stress) }}>{fmt1(row.avg_stress)}</td>
              <td style={divergingBarStyle(row.trend, -30, 30, '#2a78d6', '#e34948')}>{fmtSigned1(row.trend)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Callout variant="caption">
        Trend = avg stress in the second half of the date range minus the first half &mdash; negative means
        getting less stressful over time. Needs at least 2 meetings in each half to compute; shown as
        &ldquo;&ndash;&rdquo; otherwise, not guessed from too little data. Click a row to see their full history.
      </Callout>
    </>
  )
}
