export function shortId(id: string | null | undefined, head = 8): string {
  if (!id) return '—'
  return id.length <= head * 2 ? id : `${id.slice(0, head)}…`
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function formatClock(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

export function formatDuration(from: string | null | undefined, to?: string | null): string {
  if (!from) return '—'
  const start = new Date(from).getTime()
  if (Number.isNaN(start)) return '—'
  const endRaw = to ?? null
  const end = endRaw ? new Date(endRaw).getTime() : Date.now()
  if (Number.isNaN(end)) return '—'
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return `${minutes}m${String(rest).padStart(2, '0')}s`
}

export function relativeTime(timestamp: number | null): string {
  if (!timestamp) return '从未'
  const delta = Math.round((Date.now() - timestamp) / 1000)
  if (delta < 3) return '刚刚'
  if (delta < 60) return `${delta}s 前`
  return `${Math.floor(delta / 60)}m 前`
}

export function stringifyPayload(payload: unknown): string {
  try {
    return JSON.stringify(payload, null, 2)
  } catch {
    return String(payload)
  }
}

/** 从无类型的事件 payload 里安全取字符串，避免猜类型。 */
export function payloadString(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key]
  return typeof value === 'string' ? value : null
}
