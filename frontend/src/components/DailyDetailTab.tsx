// Port of dashboard.py's render_daily_tab.

import { useEffect, useState } from 'react'
import { fetchDaily, getDaily } from '../api'
import type { DailyResponse } from '../types'
import { addDaysIso, minIso, todayIso } from '../dateUtils'
import { fmt0, fmt1, severityTint } from '../severity'
import Callout from './Callout'
import StatCard from './StatCard'
import ChartFigure from './ChartFigure'

export default function DailyDetailTab() {
  const [date, setDate] = useState(todayIso())
  const [data, setData] = useState<DailyResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError(null)
    getDaily(date)
      .then((res) => {
        if (!cancelled) setData(res)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load daily detail.')
      })
    return () => {
      cancelled = true
    }
  }, [date])

  async function handleFetch() {
    setBusy(true)
    setError(null)
    try {
      const res = await fetchDaily(date)
      setData(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Analysis failed.')
    } finally {
      setBusy(false)
    }
  }

  const today = todayIso()
  const isToday = date >= today

  return (
    <div className="tab-panel">
      <div className="daily-controls">
        <button className="button button-secondary" title="Previous day" onClick={() => setDate((d) => minIso(addDaysIso(d, -1), today))}>
          ◀
        </button>
        <label className="field">
          <span className="field-label">Date</span>
          <input type="date" value={date} max={today} onChange={(e) => setDate(e.target.value)} />
        </label>
        <button
          className="button button-secondary"
          title="Next day"
          disabled={isToday}
          onClick={() => setDate((d) => minIso(addDaysIso(d, 1), today))}
        >
          ▶
        </button>
        <button
          className={`button ${data?.has_cached ? 'button-secondary' : 'button-primary'}`}
          onClick={handleFetch}
          disabled={busy}
        >
          {busy ? `Logging into Garmin and correlating ${date}…` : data?.has_cached ? 'Re-fetch from Garmin' : 'Fetch from Garmin'}
        </button>

        {data && data.metrics && (
          <>
            <StatCard label="Workday average stress" value={fmt1(data.metrics.avg)} />
            <StatCard label="Workday peak stress" value={fmt0(data.metrics.peak)} />
          </>
        )}
      </div>

      {error && <Callout variant="error">{error}</Callout>}

      {!error && data === null && <Callout variant="caption">Loading…</Callout>}

      {!error && data !== null && !data.has_cached && (
        <Callout variant="info">
          No stored results for {date} yet. Click <strong>Fetch from Garmin</strong> to run it.
        </Callout>
      )}

      {!error && data !== null && data.has_cached && (
        <>
          <hr />
          {data.chart ? (
            <>
              <ChartFigure figure={data.chart} height={420} />
              <Callout variant="caption">
                Hover anywhere on the chart for the exact time, stress level, and which meeting (or{' '}
                <strong>No Meeting</strong>) you were in at that moment.
              </Callout>
            </>
          ) : (
            <Callout variant="info">No data for this date.</Callout>
          )}

          <h3>Stress by meeting</h3>
          {data.event_summary.length === 0 ? (
            <Callout variant="info">No valid stress readings for this date.</Callout>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Avg stress</th>
                  <th>Peak stress</th>
                  <th>Minutes</th>
                </tr>
              </thead>
              <tbody>
                {data.event_summary.map((row) => (
                  <tr key={row.event}>
                    <td>{row.event}</td>
                    <td style={{ background: severityTint(row.avg_stress) }}>{fmt1(row.avg_stress)}</td>
                    <td style={{ background: severityTint(row.peak_stress) }}>{fmt0(row.peak_stress)}</td>
                    <td>{fmt0(row.minutes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}
