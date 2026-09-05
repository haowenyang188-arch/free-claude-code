import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ResourceState } from '../../hooks/useResource'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { PipelinePanel } from './PipelinePanel'
import type { AttemptChainResponse } from '../../types/sop'

function emptyState<T>(): ResourceState<T> {
  return {
    data: undefined,
    error: undefined,
    loading: false,
    refreshing: false,
    lastUpdated: null,
    refresh: vi.fn(),
  }
}

function bundle(chainState: ResourceState<AttemptChainResponse>): SopRunBundle {
  return {
    runId: 'run-1',
    enabled: true,
    run: emptyState(),
    steps: emptyState(),
    tasks: { ...emptyState(), data: [] },
    artifacts: emptyState(),
    attempts: emptyState(),
    evidence: emptyState(),
    chain: chainState,
    handoffs: emptyState(),
    events: emptyState(),
    trace: emptyState(),
    firstError: undefined,
    refreshAll: vi.fn(),
  }
}

const CYCLE_CHAIN: AttemptChainResponse = {
  attempts: [
    {
      id: 'att1', task_id: 't1', sequence: 1, session_id: null, runtime_id: 'fake',
      previous_attempt_id: 'att2', started_at: '2026-08-31T00:00:00Z', completed_at: null,
      status: 'accepted', sop_run_id: 'run-1', artifact_ids: [],
    },
    {
      id: 'att2', task_id: 't1', sequence: 1, session_id: null, runtime_id: 'fake',
      previous_attempt_id: 'att1', started_at: '2026-08-31T00:00:00Z', completed_at: null,
      status: 'accepted', sop_run_id: 'run-1', artifact_ids: [],
    },
  ],
  root_task_id: 't1',
  chain_length: 2,
  truncated: true,
}

describe('PipelinePanel · 重试链', () => {
  it('truncated=true 时显式提示链不完整', () => {
    render(
      <PipelinePanel
        bundle={bundle({ ...emptyState<AttemptChainResponse>(), data: CYCLE_CHAIN })}
      />,
    )
    expect(screen.getByText(/previous_attempt_id 存在环路/)).toBeInTheDocument()
  })

  it('chain 为空时显示空态', () => {
    render(
      <PipelinePanel
        bundle={bundle({
          ...emptyState<AttemptChainResponse>(),
          data: { attempts: [], root_task_id: '', chain_length: 0, truncated: false },
        })}
      />,
    )
    expect(screen.getByText('该 run 暂无 Attempt 血缘链。')).toBeInTheDocument()
  })

  it('chain 加载中显示 loading', () => {
    render(
      <PipelinePanel
        bundle={bundle({ ...emptyState<AttemptChainResponse>(), loading: true })}
      />,
    )
    expect(screen.getAllByText('加载中…').length).toBeGreaterThan(0)
  })
})
