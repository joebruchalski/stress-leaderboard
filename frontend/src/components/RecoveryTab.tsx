// Port of dashboard.py's render_recovery_tab.

import { useEffect, useState } from 'react'
import { getRecovery } from '../api'
import type { RecoveryResponse } from '../types'
import { daysAgoIso, todayIso } from '../dateUtils'
import { fmt0, fmt1 } from '../severity'
import DateRange from './DateRange'
import Callout from './Callout'
import ChartFigure from './ChartFigure'

export default function RecoveryTab() {
  const [start, setStart] = useState(daysAgoIso(13))
  const [end, setEnd] = useState(todayIso())
  const [data, setData] = useState<RecoveryResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const rangeInvalid = start > end

  useEffect(() => {
    if (rangeInvalid) return
    let cancelled = false
    setData(null)
    setError(null)
    getRecovery(start, end)
      .then((res) => {
        if (!cancelled) setData(res)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load recovery data.')
      })
    return () => {
      cancelled = true
    }
  }, [start, end, rangeInvalid])

  return (
    <div className="tab-panel">
      <DateRange start={start} end={end} onStartChange={setStart} onEndChange={setEnd} max={todayIso()} />

      {rangeInvalid && <Callout variant="error">Start date must be before end date.</Callout>}
      {!rangeInvalid && error && <Callout variant="error">Could not load recovery data: {error}</Callout>}
      {!rangeInvalid && !error && data === null && <Callout variant="caption">Loading…</Callout>}

      {!rangeInvalid && data !== null && data.rows.length === 0 && (
        <Callout variant="info">
          No sleep/Body Battery data stored for this range yet. Run an analysis (Daily Detail tab, or the
          automated job) on a day with synced Garmin sleep data first.
        </Callout>
      )}

      {!rangeInvalid && data !== null && data.rows.length > 0 && (
        <>
          <Callout variant="caption">
            Each dot is one day: does a worse night's sleep or a lower Body Battery line up with a
            higher-stress workday?
          </Callout>

          <div className="chart-pair">
            <div>
              {data.sleep_chart ? (
                <ChartFigure figure={data.sleep_chart} />
              ) : (
                <Callout variant="info">No overlapping sleep score + stress data in this range.</Callout>
              )}
            </div>
            <div>
              {data.battery_chart ? (
                <ChartFigure figure={data.battery_chart} />
              ) : (
                <Callout variant="info">No overlapping Body Battery + stress data in this range.</Callout>
              )}
            </div>
          </div>

          <h3>Recovery and stress by day</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Sleep score</th>
                <th>Total sleep minutes</th>
                <th>Body battery low</th>
                <th>Body battery high</th>
                <th>Overall avg</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.date}>
                  <td>{row.date}</td>
                  <td>{fmt0(row.sleep_score)}</td>
                  <td>{fmt0(row.total_sleep_minutes)}</td>
                  <td>{fmt0(row.body_battery_low)}</td>
                  <td>{fmt0(row.body_battery_high)}</td>
                  <td>{fmt1(row.overall_avg)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
