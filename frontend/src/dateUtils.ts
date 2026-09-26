export function todayIso(): string {
  const d = new Date()
  return toIso(d)
}

export function toIso(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function daysAgoIso(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return toIso(d)
}

export function addDaysIso(iso: string, delta: number): string {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + delta)
  return toIso(d)
}

export function minIso(a: string, b: string): string {
  return a < b ? a : b
}
