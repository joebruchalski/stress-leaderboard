// Typed API client for the FastAPI backend. Every function returns a typed
// Promise and throws an Error (with the backend's `detail` message, when
// present) on a non-2xx response, so callers can just try/catch and show
// `err.message` inline rather than re-parsing responses everywhere.

import type {
  BackfillStartResponse,
  BackfillStatusResponse,
  ConfigRequest,
  ConfigResponse,
  DailyResponse,
  HealthResponse,
  LeaderboardChartResponse,
  LeaderboardResponse,
  MostImprovedResponse,
  PersonHistoryResponse,
  RecoveryResponse,
  TestCalendarResponse,
  TrendsResponse,
} from './types'

class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') {
        detail = body.detail
      }
    } catch {
      // response body wasn't JSON — fall back to the generic message above
    }
    throw new ApiError(detail)
  }
  return (await res.json()) as T
}

function qs(params: Record<string, string | undefined>): string {
  const usable = Object.entries(params).filter(([, v]) => v !== undefined) as [string, string][]
  return usable.length ? `?${new URLSearchParams(usable).toString()}` : ''
}

export function getConfig(): Promise<ConfigResponse> {
  return request<ConfigResponse>('/api/config')
}

export function testCalendarUrl(icsUrl: string): Promise<TestCalendarResponse> {
  return request<TestCalendarResponse>('/api/config/test-calendar', {
    method: 'POST',
    body: JSON.stringify({ ics_url: icsUrl }),
  })
}

export function saveConfig(body: ConfigRequest): Promise<ConfigResponse> {
  return request<ConfigResponse>('/api/config', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function getLeaderboard(start: string, end: string): Promise<LeaderboardResponse> {
  return request<LeaderboardResponse>(`/api/leaderboard${qs({ start, end })}`)
}

export function getLeaderboardChart(
  start: string,
  end: string,
  minMeetings: number,
): Promise<LeaderboardChartResponse> {
  return request<LeaderboardChartResponse>(
    `/api/leaderboard/chart${qs({ start, end, min_meetings: String(minMeetings) })}`,
  )
}

export function getMostImproved(start: string, end: string): Promise<MostImprovedResponse> {
  return request<MostImprovedResponse>(`/api/most-improved${qs({ start, end })}`)
}

export function getPersonHistory(
  email: string,
  name: string,
  start: string,
  end: string,
): Promise<PersonHistoryResponse> {
  return request<PersonHistoryResponse>(`/api/person-history${qs({ email, name, start, end })}`)
}

export function getRecovery(start: string, end: string): Promise<RecoveryResponse> {
  return request<RecoveryResponse>(`/api/recovery${qs({ start, end })}`)
}

export function getDaily(date: string): Promise<DailyResponse> {
  return request<DailyResponse>(`/api/daily${qs({ date })}`)
}

export function fetchDaily(date: string): Promise<DailyResponse> {
  return request<DailyResponse>('/api/daily/fetch', {
    method: 'POST',
    body: JSON.stringify({ date }),
  })
}

export function getTrends(start: string, end: string): Promise<TrendsResponse> {
  return request<TrendsResponse>(`/api/trends${qs({ start, end })}`)
}

export function startBackfill(daysBack: number, forceRefresh: boolean): Promise<BackfillStartResponse> {
  return request<BackfillStartResponse>('/api/backfill', {
    method: 'POST',
    body: JSON.stringify({ days_back: daysBack, force_refresh: forceRefresh }),
  })
}

export function getBackfillStatus(jobId: string): Promise<BackfillStatusResponse> {
  return request<BackfillStatusResponse>(`/api/backfill/status${qs({ job_id: jobId })}`)
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}
