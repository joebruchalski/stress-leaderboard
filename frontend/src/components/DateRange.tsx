// Port of the "From" / "To" st.date_input pair used at the top of the
// Leaderboard, Recovery, and Trends tabs.

interface DateRangeProps {
  start: string
  end: string
  onStartChange: (value: string) => void
  onEndChange: (value: string) => void
  max?: string
}

export default function DateRange({ start, end, onStartChange, onEndChange, max }: DateRangeProps) {
  return (
    <div className="date-range-row">
      <label className="field">
        <span className="field-label">From</span>
        <input type="date" value={start} max={max} onChange={(e) => onStartChange(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">To</span>
        <input type="date" value={end} max={max} onChange={(e) => onEndChange(e.target.value)} />
      </label>
    </div>
  )
}
