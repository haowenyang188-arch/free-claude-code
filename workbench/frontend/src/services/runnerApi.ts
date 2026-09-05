export type RunnerChannel = 'xhs' | 'douyin'
export type RunnerStatus = 'starting' | 'running' | 'completed' | 'failed' | 'stopped'

export interface RunnerStartRequest {
  channel: RunnerChannel
  boot: boolean
  limit: number
  restart: boolean
  smoke: boolean
  kill_chrome: boolean
}

export interface RunnerHealth {
  status: 'ok'
  runner_version: string
  channels: RunnerChannel[]
  scraper_root: string
}

export interface RunnerRun {
  run_id: string
  channel: RunnerChannel
  status: RunnerStatus
  pid: number | null
  started_at: string
  finished_at: string | null
  exit_code: number | null
  argv: string[]
  output_files: string[]
  registered: boolean
  imported: boolean
  chrome_booted: boolean
  chrome_cleanup_error: string | null
}

export interface RunnerLogs {
  run_id: string
  lines: string[]
  next: number
  done: boolean
}

export interface RunnerImportResult extends RunnerRun {
  output_file: string
  record_count: number
  sha256: string
}

type RunnerWirePayload = Record<string, unknown>

const LEGACY_CHANNELS: Record<RunnerChannel, string> = {
  xhs: '小红书-9222',
  douyin: '抖音-9224',
}

let runnerDialect: 'modern' | 'legacy' = 'modern'

export const RUNNER_BASE = (
  import.meta.env.VITE_RUNNER_URL || 'https://127.0.0.1:8790'
).replace(/\/$/, '')

function runnerError(status: number, detail: unknown): Error & { status?: number; code?: string } {
  const body = detail && typeof detail === 'object' ? detail as RunnerWirePayload : null
  const nested = body?.error
  const errorDetail = nested && typeof nested === 'object' ? nested as RunnerWirePayload : null
  const message = typeof nested === 'string'
    ? nested
    : (typeof errorDetail?.message === 'string' ? errorDetail.message : null)
      || (typeof body?.detail === 'string' ? body.detail : null)
      || `Runner request failed (${status})`
  const error = new Error(message) as Error & { status?: number; code?: string }
  error.status = status
  error.code = typeof errorDetail?.code === 'string'
    ? errorDetail.code
    : (typeof body?.code === 'string' ? body.code : undefined)
  return error
}

export async function runnerRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${RUNNER_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  const detail = await response.json().catch(() => null) as unknown
  const body = detail && typeof detail === 'object' ? detail as RunnerWirePayload : null
  if (!response.ok || body?.ok === false) {
    throw runnerError(response.status, detail)
  }
  return detail as T
}

function unwrap(payload: unknown): RunnerWirePayload {
  if (!payload || typeof payload !== 'object') return {}
  const body = payload as RunnerWirePayload
  const nested = body.run
  return nested && typeof nested === 'object' ? nested as RunnerWirePayload : body
}

function normalizeChannel(value: unknown, fallback?: RunnerChannel): RunnerChannel {
  if (value === '小红书-9222') return 'xhs'
  if (value === '抖音-9224') return 'douyin'
  if (value === 'xhs' || value === 'douyin') return value
  if (fallback) return fallback
  throw new Error('Runner returned an unsupported channel')
}

function normalizeStatus(value: unknown, exitCode: unknown): RunnerRun['status'] {
  if (value === 'exited') return exitCode === 0 ? 'completed' : 'failed'
  if (value === 'starting' || value === 'running' || value === 'completed' || value === 'failed' || value === 'stopped') {
    return value
  }
  return exitCode === 0 ? 'completed' : 'failed'
}

function normalizeRun(payload: unknown, fallbackChannel?: RunnerChannel): RunnerRun {
  const raw = unwrap(payload)
  const exitCode = typeof raw.exit_code === 'number' ? raw.exit_code : null
  const output = raw.output_files
  const outputFiles = Array.isArray(output)
    ? output.filter((item): item is string => typeof item === 'string')
    : (typeof raw.out === 'string' ? [raw.out] : [])
  const argv = Array.isArray(raw.argv)
    ? raw.argv.filter((item): item is string => typeof item === 'string')
    : (typeof raw.cmd === 'string' ? [raw.cmd] : [])
  return {
    run_id: typeof raw.run_id === 'string' ? raw.run_id : String(raw.id || ''),
    channel: normalizeChannel(raw.channel, fallbackChannel),
    status: normalizeStatus(raw.status, exitCode),
    pid: typeof raw.pid === 'number' ? raw.pid : null,
    started_at: typeof raw.started_at === 'string' ? raw.started_at : '',
    finished_at: typeof raw.finished_at === 'string'
      ? raw.finished_at
      : (typeof raw.ended_at === 'string' ? raw.ended_at : null),
    exit_code: exitCode,
    argv,
    output_files: outputFiles,
    registered: raw.registered === true,
    imported: raw.imported === true,
    chrome_booted: raw.chrome_booted === true || raw.boot_ok === true,
    chrome_cleanup_error: typeof raw.chrome_cleanup_error === 'string' ? raw.chrome_cleanup_error : null,
  }
}

function normalizeHealth(payload: unknown): RunnerHealth {
  const raw = payload && typeof payload === 'object' ? payload as RunnerWirePayload : {}
  const channels = Array.isArray(raw.channels)
    ? raw.channels.filter((item): item is RunnerChannel => item === 'xhs' || item === 'douyin')
    : []
  if (raw.service === 'local_runner') runnerDialect = 'legacy'
  if (channels.length) runnerDialect = 'modern'
  return {
    status: 'ok',
    runner_version: typeof raw.runner_version === 'string'
      ? raw.runner_version
      : (typeof raw.service === 'string' ? raw.service : 'local_runner'),
    channels,
    scraper_root: typeof raw.scraper_root === 'string'
      ? raw.scraper_root
      : (typeof raw.scraper_dir === 'string' ? raw.scraper_dir : ''),
  }
}

async function legacyHealth(payload: unknown): Promise<RunnerHealth> {
  const health = normalizeHealth(payload)
  if (health.channels.length) return health
  try {
    const tasks = await runnerRequest<RunnerWirePayload>('/tasks')
    const rawTasks = Array.isArray(tasks.tasks) ? tasks.tasks : []
    const channels = rawTasks
      .map((task) => task && typeof task === 'object' ? (task as RunnerWirePayload).channel : null)
      .map((item) => item === '小红书-9222' ? 'xhs' : item === '抖音-9224' ? 'douyin' : null)
      .filter((item): item is RunnerChannel => item === 'xhs' || item === 'douyin')
    return { ...health, channels: [...new Set(channels)] }
  } catch {
    return { ...health, channels: ['xhs', 'douyin'] }
  }
}

export const runnerApi = {
  health: async () => legacyHealth(await runnerRequest<RunnerWirePayload>('/health')),

  start: async (request: RunnerStartRequest) => {
    const wireRequest = {
      ...request,
      channel: runnerDialect === 'legacy' ? LEGACY_CHANNELS[request.channel] : request.channel,
    }
    try {
      return normalizeRun(await runnerRequest<RunnerWirePayload>('/runs/start', {
      method: 'POST',
      body: JSON.stringify(wireRequest),
      }), request.channel)
    } catch (error) {
      // A legacy runner may be reached before its health probe completes.
      if (runnerDialect === 'modern' && (error as { status?: number }).status === 403) {
        runnerDialect = 'legacy'
        return normalizeRun(await runnerRequest<RunnerWirePayload>('/runs/start', {
          method: 'POST',
          body: JSON.stringify({ ...request, channel: LEGACY_CHANNELS[request.channel] }),
        }), request.channel)
      }
      throw error
    }
  },

  getRun: async (runId: string) => normalizeRun(
    await runnerRequest<RunnerWirePayload>(`/runs/${encodeURIComponent(runId)}${runnerDialect === 'legacy' ? '?verify=1' : ''}`),
  ),

  getLogs: async (runId: string, after = 0) => {
    const payload = await runnerRequest<RunnerWirePayload>(
      `/runs/${encodeURIComponent(runId)}/logs?${runnerDialect === 'legacy' ? 'offset' : 'after'}=${after}`,
    )
    const raw = unwrap(payload)
    const lines = Array.isArray(raw.lines)
      ? raw.lines.filter((item): item is string => typeof item === 'string')
      : []
    return {
      run_id: typeof raw.run_id === 'string' ? raw.run_id : String(raw.id || runId),
      lines,
      next: typeof raw.next === 'number'
        ? raw.next
        : (typeof raw.next_offset === 'number' ? raw.next_offset : after + lines.length),
      done: raw.done === true,
    }
  },

  stop: async (runId: string, killChrome: boolean) => normalizeRun(
    await runnerRequest<RunnerWirePayload>(`/runs/${encodeURIComponent(runId)}/stop`, {
      method: 'POST',
      body: JSON.stringify({ kill_chrome: killChrome }),
    }),
  ),

  register: async (runId: string) => normalizeRun(
    await runnerRequest<RunnerWirePayload>(`/runs/${encodeURIComponent(runId)}/register`, {
      method: 'POST',
    }),
  ),

  importOutput: async (runId: string) => {
    const payload = await runnerRequest<RunnerWirePayload>(`/runs/${encodeURIComponent(runId)}/import`, {
      method: 'POST',
    })
    const raw = unwrap(payload)
    const body = payload && typeof payload === 'object' ? payload as RunnerWirePayload : {}
    const value = (key: string) => body[key] ?? raw[key]
    return {
      ...normalizeRun(payload),
      output_file: typeof value('output_file') === 'string' ? value('output_file') as string : '',
      record_count: typeof value('record_count') === 'number' ? value('record_count') as number : 0,
      sha256: typeof value('sha256') === 'string' ? value('sha256') as string : '',
    }
  },
}
