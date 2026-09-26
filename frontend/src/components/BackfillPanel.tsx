// Port of dashboard.py's render_backfill_control — a collapsed-by-default
// section above the tabs for pulling in months of history at once.

import { useEffect, useRef, useState } from 'react'
import { getBackfillStatus, startBackfill } from '../api'
import type { BackfillFailure, BackfillStatusResponse } from '../types'
import Callout from './Callout'

export default function BackfillPanel() {
  const [open, setOpen] = useState(false)
  const [daysBack, setDaysBack] = useState(180)
  const [forceRefresh, setForceRefresh] = useState(false)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [nothingToDoMessage, setNothingToDoMessage] = useState<string | null>(null)
  const [runningTotal, setRunningTotal] = useState<{ total: number; alreadyCached: number } | null>(null)
  const [status, setStatus] = useState<BackfillStatusResponse | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [])

  function pollStatus(jobId: string) {
    getBackfillStatus(jobId)
      .then((s) => {
        setStatus(s)
        if (s.state === 'running') {
          pollRef.current = setTimeout(() => pollStatus(jobId), 1500)
        }
      })
      .catch((err) => {
        setStartError(err instanceof Error ? err.message : 'Failed to check backfill status.')
      })
  }

  async function handleStart() {
    setStarting(true)
    setStartError(null)
    setNothingToDoMessage(null)
    setStatus(null)
    setRunningTotal(null)
    try {
      const res = await startBackfill(daysBack, forceRefresh)
      if (res.total === 0) {
        setNothingToDoMessage(`Nothing to do — all ${res.already_cached} day(s) in that range are already stored.`)
        return
      }
      setRunningTotal({ total: res.total, alreadyCached: res.already_cached })
      pollStatus(res.job_id)
    } catch (err) {
      setStartError(err instanceof Error ? err.message : 'Backfill aborted before it could start.')
    } finally {
      setStarting(false)
    }
  }

  const isRunning = status?.state === 'running'
  const progress = status && status.total > 0 ? status.index / status.total : 0

  return (
    <details className="backfill-panel" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>📥 Backfill history (e.g. pull in the last 6 months)</summary>
      <div className="backfill-body">
        <Callout variant="caption">
          Fetches Garmin + calendar data for a range of past days, skipping any day already stored. A large
          range (months) makes many Garmin API calls in one run and can take several minutes &mdash; this paces
          itself between days to avoid Garmin's rate limits, but there's no way to make months of history
          arrive instantly.
        </Callout>

        <label className="field">
          <span className="field-label">Days back</span>
          <input
            type="number"
            min={1}
            max={365}
            step={1}
            value={daysBack}
            onChange={(e) => setDaysBack(Math.max(1, Math.min(365, Number(e.target.value) || 1)))}
          />
          <span className="field-help">180 ≈ 6 months, 365 = a full year.</span>
        </label>

        <label className="checkbox-field">
          <input type="checkbox" checked={forceRefresh} onChange={(e) => setForceRefresh(e.target.checked)} />
          <span>Re-fetch days that are already stored</span>
        </label>

        <button className="button button-primary" onClick={handleStart} disabled={starting || isRunning}>
          Start backfill
        </button>

        {startError && <Callout variant="error">{startError}</Callout>}
        {nothingToDoMessage && <Callout variant="success">{nothingToDoMessage}</Callout>}

        {runningTotal && status && (
          <div className="backfill-progress">
            <p>
              Fetching {runningTotal.total} day(s) ({runningTotal.alreadyCached} already cached, skipped)...
            </p>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${Math.round(progress * 100)}%` }} />
            </div>
            {isRunning && (
              <p className="caption">
                [{status.index + 1}/{status.total}] {status.current_date}...
              </p>
            )}
            {status.state === 'done' && (
              <>
                <Callout variant="success">Backfilled {status.ok_count} day(s).</Callout>
                {status.failed.length > 0 && <FailedDetails failed={status.failed} />}
              </>
            )}
            {status.state === 'error' && (
              <Callout variant="error">Backfill aborted before it could start: {status.error}</Callout>
            )}
          </div>
        )}
      </div>
    </details>
  )
}

function FailedDetails({ failed }: { failed: BackfillFailure[] }) {
  const sorted = [...failed].sort((a, b) => (a.date < b.date ? 1 : -1))
  return (
    <details className="failed-details">
      <summary>
        {failed.length} day(s) had no usable data (e.g. not synced) &mdash; click for details
      </summary>
      <div className="failed-list">
        {sorted.map((f) => (
          <div key={f.date} className="failed-row">
            {f.date}: {f.reason}
          </div>
        ))}
      </div>
    </details>
  )
}
