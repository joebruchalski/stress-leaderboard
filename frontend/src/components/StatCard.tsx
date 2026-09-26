// Port of Streamlit's st.metric — a label, a big value, and an optional
// signed delta (colored green/red, optionally inverted so "more" reads bad).

interface StatCardProps {
  label: string
  value: string
  delta?: string
  deltaInverse?: boolean
  help?: string
}

export default function StatCard({ label, value, delta, deltaInverse = false }: StatCardProps) {
  let deltaClass = ''
  if (delta !== undefined) {
    const isNegative = delta.trim().startsWith('-')
    const isPositive = delta.trim().startsWith('+')
    const goodClass = deltaInverse ? isNegative : isPositive
    const badClass = deltaInverse ? isPositive : isNegative
    deltaClass = goodClass ? 'stat-delta-good' : badClass ? 'stat-delta-bad' : ''
  }
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {delta !== undefined && <div className={`stat-delta ${deltaClass}`}>{delta}</div>}
    </div>
  )
}
