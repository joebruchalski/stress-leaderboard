// Port of dashboard.py's render_trends_tab + render_week_over_week_stat.

import { useEffect, useState } from 'react'
import { getTrends } from '../api'
import type { TrendsResponse } from '../types'
import { daysAgoIso, todayIso } from '../dateUtils'
import { fmt0, fmt1, fmtSigned1 } from '../severity'
import DateRange from './DateRange'
import Callout from './Callout'
import StatCard from './StatCard'
import ChartFigure from './ChartFigure'

export default function TrendsTab() {
  const [start, setStart] = useState(daysAgoIso(13))
  const [end, setEnd] = useState(todayIso())
  const [data, setData] = useState<TrendsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const rangeInvalid = start > end

  useEffect(() => {
    if (rangeInvalid) return
    let cancelled = false
    setData(null)
    setError(null)
    getTrends(start, end)
      .then((res) => {
        if (!cancelled) setData(res)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load trends.')
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
      {!rangeInvalid && !error && data === null && <Callout variant="caption">Loading…</Callout>}

      {!rangeInvalid && data !== null && (
        <>
          <WeekOverWeekStat weekOverWeek={data.week_over_week} />

          {!data.daily_summary_chart ? (
            <Callout variant="info">
              No stored results in this range yet. Run some daily analyses first (Daily Detail tab, or the
              automated job).
            </Callout>
          ) : (
            <>
              <ChartFigure figure={data.daily_summary_chart} />

              <h3>Average stress by meeting, across this range</h3>
              {data.event_rollup.length === 0 ? (
                <Callout variant="info">No meeting data in this range.</Callout>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Event</th>
                      <th>Avg stress</th>
                      <th>Peak stress</th>
                      <th>Total minutes</th>
                      <th>Occurrences</th>
                      <th>Avg Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.event_rollup.map((row) => (
                      <tr key={row.event}>
                        <td>{row.event}</td>
                        <td>{fmt1(row.avg_stress)}</td>
                        <td>{fmt0(row.peak_stress)}</td>
                        <td>{fmt0(row.total_minutes)}</td>
                        <td>{fmt0(row.occurrences)}</td>
                        <td>{fmtSigned1(row.avg_delta)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <h3>Meeting size vs. stress</h3>
              <Callout variant="caption">
                Does attendee count line up with higher stress &mdash; are big group calls worse than small
                ones?
              </Callout>
              {data.meeting_size_chart ? (
                <ChartFigure figure={data.meeting_size_chart} />
              ) : (
                <Callout variant="info">No meetings with stored attendee data in this range.</Callout>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

function WeekOverWeekStat({ weekOverWeek }: { weekOverWeek: TrendsResponse['week_over_week'] }) {
  if (weekOverWeek.recent_avg === null) {
    return <Callout variant="caption">Not enough recent history yet for a week-over-week comparison.</Callout>
  }
  return (
    <div
      className="stat-row"
      title="Average of daily overall_avg stress over the last 7 days vs. the 7 days before that. No comparison shown if last week has no stored data."
    >
      <StatCard
        label="This week's average stress vs. last week"
        value={fmt1(weekOverWeek.recent_avg)}
        delta={weekOverWeek.delta !== null ? fmtSigned1(weekOverWeek.delta) : undefined}
        deltaInverse
      />
    </div>
  )
}
