// Port of dashboard.py's render_leaderboard_tab — the primary/default tab.

import { useEffect, useState } from 'react'
import { getLeaderboard, getLeaderboardChart } from '../api'
import type { LeaderboardRow, PlotlyFigureJSON } from '../types'
import { daysAgoIso, todayIso } from '../dateUtils'
import { blueTint, divergingBarStyle, fmt0, fmt1, fmtComma0, fmtSigned1, severityTint } from '../severity'
import DateRange from './DateRange'
import Callout from './Callout'
import ChartFigure from './ChartFigure'
import StatCard from './StatCard'
import MostImprovedPanel from './MostImprovedPanel'
import PersonDrilldown from './PersonDrilldown'

type RankBy = 'avg' | 'total' | 'delta'

export default function LeaderboardTab() {
  const [start, setStart] = useState(daysAgoIso(89))
  const [end, setEnd] = useState(todayIso())
  const [rows, setRows] = useState<LeaderboardRow[] | null>(null)
  const [maxMeetings, setMaxMeetings] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [minMeetings, setMinMeetings] = useState(1)
  const [rankBy, setRankBy] = useState<RankBy>('avg')
  const [selected, setSelected] = useState<{ email: string; name: string } | null>(null)

  const rangeInvalid = start > end

  useEffect(() => {
    if (rangeInvalid) return
    let cancelled = false
    setRows(null)
    setError(null)
    getLeaderboard(start, end)
      .then((res) => {
        if (cancelled) return
        setRows(res.rows)
        setMaxMeetings(res.max_meetings)
        setMinMeetings(res.max_meetings && res.max_meetings > 1 ? Math.min(2, res.max_meetings) : 1)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load leaderboard.')
      })
    return () => {
      cancelled = true
    }
  }, [start, end, rangeInvalid])

  return (
    <div className="tab-panel">
      <DateRange start={start} end={end} onStartChange={setStart} onEndChange={setEnd} max={todayIso()} />

      {rangeInvalid && <Callout variant="error">Start date must be before end date.</Callout>}
      {!rangeInvalid && error && <Callout variant="error">{error}</Callout>}

      {!rangeInvalid && !error && rows === null && <Callout variant="caption">Loading…</Callout>}

      {!rangeInvalid && rows !== null && rows.length === 0 && (
        <Callout variant="info">
          No attendee data in this range yet. This needs your 'own email' set in Settings (so you can be
          excluded) and at least one analysis run in this range.
        </Callout>
      )}

      {!rangeInvalid && rows !== null && rows.length > 0 && (
        <LeaderboardBody
          rows={rows}
          maxMeetings={maxMeetings}
          minMeetings={minMeetings}
          setMinMeetings={setMinMeetings}
          rankBy={rankBy}
          setRankBy={setRankBy}
          start={start}
          end={end}
          selected={selected}
          onSelect={(email, name) => setSelected({ email, name })}
        />
      )}

      <PersonDrilldown
        email={selected?.email ?? null}
        name={selected?.name ?? null}
        start={start}
        end={end}
        onClear={() => setSelected(null)}
      />
    </div>
  )
}

function LeaderboardBody({
  rows,
  maxMeetings,
  minMeetings,
  setMinMeetings,
  rankBy,
  setRankBy,
  start,
  end,
  selected,
  onSelect,
}: {
  rows: LeaderboardRow[]
  maxMeetings: number | null
  minMeetings: number
  setMinMeetings: (n: number) => void
  rankBy: RankBy
  setRankBy: (r: RankBy) => void
  start: string
  end: string
  selected: { email: string; name: string } | null
  onSelect: (email: string, name: string) => void
}) {
  const filtered = rows.filter((r) => r.meetings >= minMeetings)

  let ranked: LeaderboardRow[]
  if (rankBy === 'avg') {
    ranked = [...filtered].sort((a, b) => b.avg_stress - a.avg_stress)
  } else if (rankBy === 'total') {
    ranked = [...filtered].sort((a, b) => b.total_stress_exposure - a.total_stress_exposure)
  } else {
    ranked = filtered
      .filter((r) => r.avg_delta !== null && r.avg_delta !== undefined)
      .sort((a, b) => (b.avg_delta as number) - (a.avg_delta as number))
  }

  return (
    <>
      {maxMeetings !== null && maxMeetings > 1 ? (
        <label className="field">
          <span className="field-label">Minimum meetings together (filters out noisy one-off large invites)</span>
          <input
            type="range"
            min={1}
            max={maxMeetings}
            value={minMeetings}
            onChange={(e) => setMinMeetings(Number(e.target.value))}
          />
          <span className="field-help">{minMeetings}</span>
        </label>
      ) : (
        <Callout variant="caption">Not enough history yet for a minimum-meetings filter &mdash; showing everyone.</Callout>
      )}

      {filtered.length === 0 ? (
        <Callout variant="info">No one meets that threshold in this range &mdash; lower the slider.</Callout>
      ) : (
        <>
          <fieldset className="radio-group">
            <legend>
              Rank by
              <span
                className="help-icon"
                title={
                  'Average stress: their meetings’ overall stress level, which can reflect a generally stressful ' +
                  'day, not just them. Total stress exposure: cumulative stress × time spent with them — rewards ' +
                  'someone who stresses you a little but constantly, not just one bad meeting. Δ (delta): how much ' +
                  'stress actually rose going into their meetings vs. right before — a more causal signal, but needs ' +
                  'enough clean before/after data.'
                }
              >
                ⓘ
              </span>
            </legend>
            <label>
              <input type="radio" checked={rankBy === 'avg'} onChange={() => setRankBy('avg')} />
              Average stress
            </label>
            <label>
              <input type="radio" checked={rankBy === 'total'} onChange={() => setRankBy('total')} />
              Total stress exposure
            </label>
            <label>
              <input type="radio" checked={rankBy === 'delta'} onChange={() => setRankBy('delta')} />
              Stress increase when the meeting starts (Δ)
            </label>
          </fieldset>

          {ranked.length === 0 ? (
            <Callout variant="info">
              No one has a computable Δ in this range yet (needs clean free time right before a meeting).
            </Callout>
          ) : (
            <LeaderboardTable
              ranked={ranked}
              selected={selected}
              onSelect={onSelect}
              start={start}
              end={end}
              minMeetings={minMeetings}
            />
          )}
        </>
      )}

      <hr />
      <MostImprovedPanel start={start} end={end} minMeetings={minMeetings} onSelect={onSelect} />
    </>
  )
}

function LeaderboardTable({
  ranked,
  selected,
  onSelect,
  start,
  end,
  minMeetings,
}: {
  ranked: LeaderboardRow[]
  selected: { email: string; name: string } | null
  onSelect: (email: string, name: string) => void
  start: string
  end: string
  minMeetings: number
}) {
  const top = ranked[0]
  const displayed = ranked.slice(0, 20)
  const maxExposure = Math.max(...displayed.map((r) => r.total_stress_exposure), 0)

  return (
    <>
      <div className="stat-row">
        <StatCard label="🏆 Top stressor" value={top.attendee_name} />
        <StatCard label="Avg stress in their meetings" value={fmt0(top.avg_stress)} />
        <StatCard label="Meetings together" value={fmt0(top.meetings)} />
        <StatCard label="Total stress exposure" value={fmtComma0(top.total_stress_exposure)} />
      </div>

      <h3>Leaderboard</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Attendee</th>
            <th>Meetings</th>
            <th>Avg stress</th>
            <th>Peak stress</th>
            <th>Total exposure</th>
            <th>Δ stress (during vs. before)</th>
          </tr>
        </thead>
        <tbody>
          {displayed.map((row, i) => (
            <tr
              key={row.attendee_email}
              className={`clickable-row${selected?.email === row.attendee_email ? ' selected-row' : ''}`}
              onClick={() => onSelect(row.attendee_email, row.attendee_name)}
            >
              <td>{i + 1}</td>
              <td>{row.attendee_name}</td>
              <td>{fmt0(row.meetings)}</td>
              <td style={{ background: severityTint(row.avg_stress) }}>{fmt1(row.avg_stress)}</td>
              <td style={{ background: severityTint(row.peak_stress) }}>{fmt0(row.peak_stress)}</td>
              <td style={{ background: blueTint(row.total_stress_exposure, maxExposure) }}>
                {fmtComma0(row.total_stress_exposure)}
              </td>
              <td style={divergingBarStyle(row.avg_delta, -30, 30, '#e34948', '#2a78d6')}>{fmtSigned1(row.avg_delta)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Callout variant="caption">
        Total exposure = avg stress × minutes, summed across every meeting with them &mdash; not capped at 100
        like the other columns, since it's a cumulative total, not a level. Click a row to see that person's
        full history below.
      </Callout>

      <ChartViewExpander start={start} end={end} minMeetings={minMeetings} />

      <Callout variant="caption">
        An event's average/peak stress applies to everyone who attended it &mdash; this shows who you're in
        stressful meetings WITH, not who specifically causes the stress within a group call.
      </Callout>
    </>
  )
}

function ChartViewExpander({ start, end, minMeetings }: { start: string; end: string; minMeetings: number }) {
  const [open, setOpen] = useState(false)
  const [chart, setChart] = useState<PlotlyFigureJSON | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setChart(undefined)
    setError(null)
    getLeaderboardChart(start, end, minMeetings)
      .then((res) => {
        if (!cancelled) setChart(res.chart)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load chart.')
      })
    return () => {
      cancelled = true
    }
  }, [open, start, end, minMeetings])

  return (
    <details className="chart-view-expander" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>Chart view (average stress)</summary>
      {error && <Callout variant="error">{error}</Callout>}
      {!error && chart === undefined && <Callout variant="caption">Loading…</Callout>}
      {!error && chart === null && <Callout variant="info">No data to chart for this range/filter.</Callout>}
      {!error && chart && <ChartFigure figure={chart} />}
    </details>
  )
}
