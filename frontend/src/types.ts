// Shared API types, matching the FastAPI backend contract exactly
// (see task spec / api.py sibling project).

export interface PlotlyFigureJSON {
  data: unknown[]
  layout: Record<string, unknown>
}

export interface ConfigResponse {
  email: string
  ics_path: string
  ics_url: string
  calendar_email: string
  internal_domain: string
  tokenstore: string
  has_password: boolean
  ready: boolean
}

export interface ConfigRequest {
  email: string
  ics_path: string
  ics_url: string
  calendar_email: string
  internal_domain: string
  password?: string
}

export interface TestCalendarResponse {
  ok: boolean
  message: string
  event_count?: number
}

export interface LeaderboardRow {
  attendee_email: string
  attendee_name: string
  meetings: number
  avg_stress: number
  peak_stress: number
  total_stress_exposure: number
  avg_delta: number | null
}

export interface LeaderboardResponse {
  max_meetings: number | null
  rows: LeaderboardRow[]
}

export interface LeaderboardChartResponse {
  chart: PlotlyFigureJSON | null
}

export interface MostImprovedRow {
  attendee_email: string
  attendee_name: string
  meetings: number
  avg_stress: number
  peak_stress: number
  trend: number | null
  first_half_avg: number | null
  second_half_avg: number | null
  first_half_meetings: number
  second_half_meetings: number
}

export interface MostImprovedResponse {
  rows: MostImprovedRow[]
}

export interface PersonHistoryRow {
  date: string
  event: string
  avg_stress: number
  peak_stress: number
  minutes: number
  delta_stress: number | null
}

export interface PersonHistoryResponse {
  rows: PersonHistoryRow[]
  chart: PlotlyFigureJSON | null
}

export interface RecoveryRow {
  date: string
  sleep_score: number | null
  total_sleep_minutes: number | null
  body_battery_low: number | null
  body_battery_high: number | null
  overall_avg: number | null
}

export interface RecoveryResponse {
  rows: RecoveryRow[]
  sleep_chart: PlotlyFigureJSON | null
  battery_chart: PlotlyFigureJSON | null
}

export interface DailyEventSummaryRow {
  event: string
  avg_stress: number
  peak_stress: number
  minutes: number
}

export interface DailyResponse {
  has_cached: boolean
  metrics: { avg: number; peak: number } | null
  event_summary: DailyEventSummaryRow[]
  chart: PlotlyFigureJSON | null
}

export interface WeekOverWeek {
  recent_avg: number | null
  previous_avg: number | null
  delta: number | null
}

export interface EventRollupRow {
  event: string
  avg_stress: number
  peak_stress: number
  total_minutes: number
  occurrences: number
  avg_delta: number | null
}

export interface TrendsResponse {
  week_over_week: WeekOverWeek
  daily_summary_chart: PlotlyFigureJSON | null
  event_rollup: EventRollupRow[]
  meeting_size_chart: PlotlyFigureJSON | null
}

export interface BackfillStartResponse {
  job_id: string
  total: number
  already_cached: number
}

export interface BackfillFailure {
  date: string
  reason: string
}

export interface BackfillStatusResponse {
  state: 'running' | 'done' | 'error'
  index: number
  total: number
  current_date: string | null
  ok_count: number
  failed: BackfillFailure[]
  error: string | null
}

export interface HealthResponse {
  status: 'ok'
}
