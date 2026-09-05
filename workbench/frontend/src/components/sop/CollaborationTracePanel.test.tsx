import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CollaborationTracePanel } from './CollaborationTracePanel'
import type { HandoffResponse, SopTraceResponse } from '../../types/sop'

const TRACE: SopTraceResponse = {
  sop_run_id: 'run-1',
  mode: 'workflow_events_only',
  provider_events_available: false,
  empty: false,
  entries: [
    {
      sequence: 1,
      event_type: 'step_started',
      role_id: 'claude',
      runtime_id: 'claude-code-provider-secret',
      phase: 'plan',
      message: 'A workflow step started.',
      tool: 'private-provider-tool',
      status: 'running',
      occurred_at: '2026-09-03T10:00:00Z',
    },
    {
      sequence: 2,
      event_type: 'handoff_created',
      role_id: 'dsh',
      runtime_id: 'dsh-runtime-secret',
      phase: 'execute',
      message: 'An Artifact handoff is ready.',
      tool: null,
      status: 'ready',
      occurred_at: '2026-09-03T10:01:00Z',
    },
    {
      sequence: 3,
      event_type: 'review_requested',
      role_id: 'codex',
      runtime_id: 'codex-provider-secret',
      phase: 'review',
      message: 'A review gate is waiting for Codex.',
      tool: null,
      status: 'waiting_review',
      occurred_at: '2026-09-03T10:02:00Z',
    },
  ],
}

const PLAN_HANDOFF: HandoffResponse = {
  id: 'handoff-plan-1',
  from_task_id: 'task-plan',
  to_step_id: 'step-execute',
  status: 'ready',
  artifact_ids: ['artifact-plan'],
  created_at: '2026-09-03T10:01:00Z',
  accepted_at: null,
  message_type: 'plan_ready',
  brief: 'Execute the approved plan',
  reply_to_handoff_id: null,
  correlation_id: 'corr-1',
  source_role_id: 'claude',
  target_role_id: 'dsh',
  source_runtime_id: 'claude-code-provider-secret',
  target_runtime_id: 'dsh-runtime-secret',
  from_role_id: 'claude',
  to_role_id: 'dsh',
  dispatchable: true,
  dispatch_reason: null,
}

function traceState(overrides: Partial<{
  data: SopTraceResponse | undefined
  error: unknown
  loading: boolean
}>) {
  return { data: undefined, error: undefined, loading: false, ...overrides }
}

describe('CollaborationTracePanel', () => {
  it('shows loading and empty states', () => {
    const { rerender } = render(
      <CollaborationTracePanel trace={traceState({ loading: true })} />,
    )
    expect(screen.getByText('加载中…')).toBeInTheDocument()

    rerender(<CollaborationTracePanel trace={traceState({ data: { ...TRACE, entries: [] } })} />)
    expect(screen.getByText('该 run 暂无协作过程记录。')).toBeInTheDocument()
  })

  it('renders all fixed phases and never exposes provider/runtime fields', () => {
    render(<CollaborationTracePanel trace={traceState({ data: TRACE })} />)

    expect(screen.getAllByText('Claude · 方案设计').length).toBeGreaterThan(0)
    expect(screen.getAllByText('DSH · 主执行').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Codex · 最终审核').length).toBeGreaterThan(0)
    expect(screen.getAllByText('A workflow step started.').length).toBeGreaterThan(0)
    expect(screen.getByText(/当前仅显示 SOP Engine 审计事件/)).toBeInTheDocument()
    expect(screen.queryByText('claude-code-provider-secret')).toBeNull()
    expect(screen.queryByText('dsh-runtime-secret')).toBeNull()
    expect(screen.queryByText('private-provider-tool')).toBeNull()
  })

  it('dispatches only a dispatchable plan_ready handoff', () => {
    const onDispatchPlan = vi.fn()
    render(
      <CollaborationTracePanel
        trace={traceState({ data: TRACE })}
        planHandoffs={[PLAN_HANDOFF]}
        onDispatchPlan={onDispatchPlan}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '发送给 DSH' }))
    expect(onDispatchPlan).toHaveBeenCalledWith(PLAN_HANDOFF)
  })

  it('keeps the dispatch button disabled when its callback is not wired', () => {
    render(
      <CollaborationTracePanel
        trace={traceState({ data: TRACE })}
        planHandoffs={[PLAN_HANDOFF]}
      />,
    )

    expect(screen.getByRole('button', { name: '发送给 DSH' })).toBeDisabled()
  })

  it('keeps a successfully dispatched handoff disabled until refreshed', () => {
    render(
      <CollaborationTracePanel
        trace={traceState({ data: TRACE })}
        planHandoffs={[PLAN_HANDOFF]}
        onDispatchPlan={vi.fn()}
        dispatchResult={{
          sop_run_id: 'run-1',
          handoff_id: PLAN_HANDOFF.id,
          status: 'dispatched',
          target_step_id: 'step-execute',
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
        }}
      />,
    )

    expect(screen.getByRole('button', { name: '已发送' })).toBeDisabled()
  })

  it('shows API errors without dropping the panel shell', () => {
    render(
      <CollaborationTracePanel
        trace={traceState({ error: new Error('trace unavailable') })}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent(/读取协作过程失败/)
    expect(screen.getByText('该 run 暂无协作过程记录。')).toBeInTheDocument()
  })
})
