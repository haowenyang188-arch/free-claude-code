import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  dispatchSopHandoff,
  getAttemptChain,
  getSopAttempts,
  getSopEvidence,
  getSopTrace,
} from './sopApi'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const VALID_ATTEMPT = {
  id: 'att1',
  task_id: 't1',
  sequence: 1,
  session_id: null,
  runtime_id: 'fake',
  previous_attempt_id: null,
  started_at: '2026-08-31T00:00:00Z',
  completed_at: null,
  status: 'accepted',
  sop_run_id: 'run-1',
  artifact_ids: [],
}

describe('sopApi · Phase 5A（运行时 DTO 校验 + 错误归一化）', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('401 / 404 / 500 → reject 标准 Error 且带 status', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(401, { detail: 'AUTH_REQUIRED' }))
    await expect(getSopAttempts('r')).rejects.toMatchObject({ status: 401 })

    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(404, { detail: 'not found' }))
    await expect(getAttemptChain('x')).rejects.toMatchObject({ status: 404 })

    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(500, { detail: 'boom' }))
    await expect(getSopEvidence('r')).rejects.toMatchObject({ status: 500 })
  })

  it('非 JSON 响应 → reject 标准 Error（不静默透传）', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('not json at all', { status: 500 }))
    await expect(getSopAttempts('r')).rejects.toBeInstanceOf(Error)
  })

  it('attempts 缺必填字段 → reject（JSON 合法但结构错）', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, [{ id: 'att1' }]))
    await expect(getSopAttempts('r')).rejects.toThrow(/Attempt/)
  })

  it('attempts 合法数组 → 原样返回', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, [VALID_ATTEMPT]))
    await expect(getSopAttempts('r')).resolves.toEqual([VALID_ATTEMPT])
  })

  it('evidence outcome 非法枚举 → reject', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, [
        {
          review_id: 'r1',
          reviewed_attempt_id: null,
          review_status: 'approved',
          outcome: 'MAYBE',
          blocking_items: [],
          outcome_parse_error: null,
          execution_diff: null,
          execution_test_report: null,
          reviewer_report: null,
          reviewer_attempt_id: null,
          evidence_valid: false,
          evidence_complete: false,
          created_at: '2026-08-31T00:00:00Z',
        },
      ]),
    )
    await expect(getSopEvidence('r')).rejects.toThrow(/枚举/)
  })

  it('evidence 嵌套 Artifact 结构非法 → reject', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, [
        {
          review_id: 'r1',
          reviewed_attempt_id: 'att1',
          review_status: 'approved',
          outcome: 'PASS',
          blocking_items: [],
          outcome_parse_error: null,
          execution_diff: { id: 'a1' }, // 缺 task_id / type 等
          execution_test_report: null,
          reviewer_report: null,
          reviewer_attempt_id: null,
          evidence_valid: true,
          evidence_complete: true,
          created_at: '2026-08-31T00:00:00Z',
        },
      ]),
    )
    await expect(getSopEvidence('r')).rejects.toThrow(/Artifact/)
  })

  it('chain 结构非法（attempts 非数组）→ reject', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, { attempts: 'nope', root_task_id: 't1', chain_length: 1, truncated: false }),
    )
    await expect(getAttemptChain('att1')).rejects.toThrow(/结构/)
  })

  it('chain 合法响应 → 原样返回', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, {
        attempts: [VALID_ATTEMPT],
        root_task_id: 't1',
        chain_length: 1,
        truncated: false,
      }),
    )
    await expect(getAttemptChain('att1')).resolves.toMatchObject({ chain_length: 1 })
  })

  it('trace 使用只读投影接口', async () => {
    const trace = {
      sop_run_id: 'run-1',
      mode: 'workflow_events_only',
      provider_events_available: false,
      empty: true,
      entries: [],
    }
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, trace))
    await expect(getSopTrace('run/1')).resolves.toEqual(trace)
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe('/api/sop-runs/run%2F1/trace')
  })

  it('dispatch 只发送 run 和 handoff ID，不接受前端改写目标', async () => {
    const result = {
      sop_run_id: 'run-1',
      handoff_id: 'handoff-1',
      status: 'dispatched',
      target_step_id: 'execute',
      target_role_id: 'dsh',
      target_runtime_id: 'dsh',
      runtime_mode: 'fake',
      task_id: 'task-1',
      attempt_id: null,
      workflow_session_id: null,
      session_id: null,
      event_id: 'event-1',
      outgoing_handoff_id: null,
      artifact_ids: [],
    }
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(200, result))
    await expect(dispatchSopHandoff('run/1', 'handoff/1')).resolves.toEqual(result)
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/sop-runs/run%2F1/handoffs/handoff%2F1/dispatch')
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
  })
})
