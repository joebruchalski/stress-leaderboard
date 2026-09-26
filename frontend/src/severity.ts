// Cell-tinting helpers, porting the coloring dashboard.py applies via
// pandas Styler (stress_core.stress_severity_css / .background_gradient /
// .bar) to plain CSS for table cells rendered in React.

import type { CSSProperties } from 'react'

/** Same scale as the backend's Plotly charts (severity color scale in the
 * task spec) — used for avg/peak stress cell tinting everywhere. ~15%
 * opacity (`26` alpha suffix) so text stays legible over the tint. */
export function severityTint(value: number | null | undefined): string | undefined {
  if (value === null || value === undefined || Number.isNaN(value)) return undefined
  if (value < 25) return '#0ca30c26'
  if (value < 50) return '#fab21926'
  if (value < 75) return '#ec835a26'
  if (value <= 100) return '#d03b3b26'
  return undefined
}

/** Blues gradient, 0..max linear opacity — port of
 * `.background_gradient(subset=["Total exposure"], cmap="Blues")`. `max`
 * should be the max value among the rows actually being displayed. */
export function blueTint(value: number | null | undefined, max: number): string | undefined {
  if (value === null || value === undefined || Number.isNaN(value) || max <= 0) return undefined
  const alpha = Math.max(0, Math.min(1, value / max)) * 0.55
  return `rgba(42, 120, 214, ${alpha.toFixed(3)})`
}

/** Reversed-green gradient (low = most saturated) — port of
 * `.background_gradient(subset=["Avg stress"], cmap="Greens_r", vmin=0, vmax=100)`
 * on the Most Improved panel, where LOW avg stress is the "good" end. */
export function greenTintInverted(value: number | null | undefined): string | undefined {
  if (value === null || value === undefined || Number.isNaN(value)) return undefined
  const clamped = Math.max(0, Math.min(100, value))
  const alpha = ((100 - clamped) / 100) * 0.55
  return `rgba(12, 163, 12, ${alpha.toFixed(3)})`
}

/** Diverging bar background, centered at zero — port of pandas Styler.bar
 * with align=0. `negativeColor` is used for values < 0, `positiveColor` for
 * values >= 0, matching the [negative, positive] order pandas' `color=[...]`
 * list takes (confirmed against dashboard.py's own comments on this). */
export function divergingBarStyle(
  value: number | null | undefined,
  vmin: number,
  vmax: number,
  negativeColor: string,
  positiveColor: string,
): CSSProperties {
  if (value === null || value === undefined || Number.isNaN(value)) return {}
  const clamped = Math.max(vmin, Math.min(vmax, value))
  const range = vmax - vmin
  const zeroPct = ((0 - vmin) / range) * 100
  const valuePct = ((clamped - vmin) / range) * 100
  const color = clamped >= 0 ? positiveColor : negativeColor
  const left = Math.min(zeroPct, valuePct)
  const right = Math.max(zeroPct, valuePct)
  return {
    backgroundImage: `linear-gradient(90deg, transparent ${left}%, ${color} ${left}%, ${color} ${right}%, transparent ${right}%)`,
  }
}

export function fmt1(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value) ? '–' : value.toFixed(1)
}

export function fmt0(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value) ? '–' : value.toFixed(0)
}

export function fmtSigned1(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  const sign = value >= 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}`
}

export function fmtComma0(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '–'
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
}
