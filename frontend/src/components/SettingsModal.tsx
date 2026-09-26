// Port of dashboard.py's render_settings_popover — a modal here instead of
// a Streamlit popover, but same fields, same help text, same validation
// messaging.

import { useEffect, useState, type FormEvent } from 'react'
import { saveConfig, testCalendarUrl } from '../api'
import type { ConfigRequest, ConfigResponse } from '../types'
import Callout from './Callout'

interface SettingsModalProps {
  config: ConfigResponse
  onClose: () => void
  onSaved: (config: ConfigResponse) => void
}

export default function SettingsModal({ config, onClose, onSaved }: SettingsModalProps) {
  const [email, setEmail] = useState(config.email)
  const [icsPath, setIcsPath] = useState(config.ics_path)
  const [icsUrl, setIcsUrl] = useState(config.ics_url)
  const [calendarEmail, setCalendarEmail] = useState(config.calendar_email)
  const [internalDomain, setInternalDomain] = useState(config.internal_domain)
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    if (!success) return
    const t = setTimeout(() => setSuccess(null), 3000)
    return () => clearTimeout(t)
  }, [success])

  async function handleTestUrl() {
    if (!icsUrl.trim()) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testCalendarUrl(icsUrl.trim())
      setTestResult({ ok: res.ok, message: res.message })
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof Error ? err.message : 'Test failed.' })
    } finally {
      setTesting(false)
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!email || (!icsPath && !icsUrl.trim())) {
      setError('Email and either a .ics path or a live calendar URL are required.')
      setSuccess(null)
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const body: ConfigRequest = {
        email,
        ics_path: icsPath,
        ics_url: icsUrl.trim(),
        calendar_email: calendarEmail,
        internal_domain: internalDomain,
      }
      if (password) body.password = password
      const updated = await saveConfig(body)
      onSaved(updated)
      setSuccess(password ? 'Settings saved. Password stored in macOS Keychain.' : 'Settings saved.')
      setPassword('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save settings.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Settings</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>
        <form onSubmit={handleSubmit} className="settings-form">
          <label className="field">
            <span className="field-label">Garmin Connect email</span>
            <input type="text" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>

          <label className="field">
            <span className="field-label">Path to .ics calendar file</span>
            <input type="text" value={icsPath} onChange={(e) => setIcsPath(e.target.value)} />
          </label>

          <label className="field">
            <span className="field-label">Live calendar URL (optional &mdash; overrides the local .ics file if set)</span>
            <div className="field-row">
              <input
                type="text"
                value={icsUrl}
                onChange={(e) => {
                  setIcsUrl(e.target.value)
                  setTestResult(null)
                }}
              />
              <button
                type="button"
                className="button"
                disabled={!icsUrl.trim() || testing}
                onClick={handleTestUrl}
              >
                {testing ? 'Testing…' : 'Test'}
              </button>
            </div>
            <span className="field-help">
              Your calendar provider's private/secret iCal address (Google Calendar, Outlook, or iCloud all
              offer one). When set, this is fetched live instead of reading the local .ics file above; if a
              fetch ever fails, the last successfully-fetched calendar is used instead, with a warning.
            </span>
            {testResult && (
              <Callout variant={testResult.ok ? 'success' : 'error'}>{testResult.message}</Callout>
            )}
          </label>

          <label className="field">
            <span className="field-label">Your own email as it appears in meeting invites</span>
            <input type="text" value={calendarEmail} onChange={(e) => setCalendarEmail(e.target.value)} />
            <span className="field-help">Excluded from the leaderboard so you don't show up as your own stressor.</span>
          </label>

          <label className="field">
            <span className="field-label">Internal email domain (e.g. yourcompany.com)</span>
            <input type="text" value={internalDomain} onChange={(e) => setInternalDomain(e.target.value)} />
            <span className="field-help">
              Leaderboard only shows attendees on this domain &mdash; leave blank to include everyone, including
              customers/vendors.
            </span>
          </label>

          <label className="field">
            <span className="field-label">Garmin Connect password</span>
            <input
              type="password"
              value={password}
              placeholder="leave blank to keep saved password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>

          <button type="submit" className="button button-primary" disabled={submitting}>
            {submitting ? 'Saving…' : 'Save settings'}
          </button>
        </form>

        {error && <Callout variant="error">{error}</Callout>}
        {success && <Callout variant="success">{success}</Callout>}
        {!config.ready && !success && (
          <Callout variant="warning">
            Fill in your Garmin email, password, and a .ics path or live calendar URL to run analyses.
          </Callout>
        )}
      </div>
    </div>
  )
}
