/**
 * SOP run 的本地选择记忆。
 *
 * `GET /api/sop-runs` 已能提供列表，但用户手动载入的 run id 仍值得记住，
 * 且刷新页面后要回到同一个 run。localStorage 不可用时静默降级。
 */

const RUNS_KEY = 'sop-console.recent-runs'
const SELECTED_KEY = 'sop-console.selected-run'

export interface RecentRun {
  id: string
  note?: string
  addedAt: number
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return fallback
    return JSON.parse(raw) as T
  } catch {
    return fallback
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* 隐私模式下不可写——历史只是不持久化，不影响功能 */
  }
}

export function loadRecentRuns(): RecentRun[] {
  const list = readJson<RecentRun[]>(RUNS_KEY, [])
  return Array.isArray(list) ? list.filter((item) => typeof item?.id === 'string') : []
}

export function rememberRun(id: string, note?: string): RecentRun[] {
  const trimmed = id.trim()
  if (!trimmed) return loadRecentRuns()
  const existing = loadRecentRuns().filter((item) => item.id !== trimmed)
  const next = [{ id: trimmed, note, addedAt: Date.now() }, ...existing].slice(0, 20)
  writeJson(RUNS_KEY, next)
  return next
}

export function forgetRun(id: string): RecentRun[] {
  const next = loadRecentRuns().filter((item) => item.id !== id)
  writeJson(RUNS_KEY, next)
  return next
}

export function loadSelectedRun(): string {
  return readJson<string>(SELECTED_KEY, '')
}

export function saveSelectedRun(id: string): void {
  writeJson(SELECTED_KEY, id)
}
