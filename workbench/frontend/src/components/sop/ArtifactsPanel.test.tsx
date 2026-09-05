import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ResourceState } from '../../hooks/useResource'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { ArtifactsPanel } from './ArtifactsPanel'
import type { ArtifactResponse } from '../../types/sop'

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

const LEGACY_ARTIFACT: ArtifactResponse = {
  id: 'a_legacy',
  task_id: 't1',
  type: 'diff',
  uri: null,
  summary: 'legacy diff',
  sha256: 'x',
  accepted: true,
  created_at: '2026-08-31T00:00:00Z',
  attempt_id: null,
  producer_step_run_id: 'sr1',
  role_id: 'dsh',
}

const NEW_ARTIFACT: ArtifactResponse = {
  ...LEGACY_ARTIFACT,
  id: 'a_new',
  summary: 'new plan',
  type: 'plan',
  attempt_id: 'att1',
}

function bundle(overrides: Partial<ResourceState<ArtifactResponse[]>>): SopRunBundle {
  return {
    runId: 'run-1',
    enabled: true,
    run: emptyState(),
    steps: emptyState(),
    tasks: { ...emptyState(), data: [] },
    artifacts: { ...emptyState<ArtifactResponse[]>(), ...overrides },
    attempts: {
      ...emptyState(),
      data: [
        {
          id: 'att1', task_id: 't1', sequence: 1, session_id: null, runtime_id: 'fake',
          previous_attempt_id: null, started_at: '2026-08-31T00:00:00Z', completed_at: null,
          status: 'accepted', sop_run_id: 'run-1', artifact_ids: ['a_new'],
        },
      ],
    },
    evidence: emptyState(),
    chain: {
      ...emptyState(),
      data: {
        attempts: [
          {
            id: 'att1', task_id: 't1', sequence: 1, session_id: null, runtime_id: 'fake',
            previous_attempt_id: null, started_at: '2026-08-31T00:00:00Z', completed_at: null,
            status: 'accepted', sop_run_id: 'run-1', artifact_ids: ['a_new'],
          },
        ],
        root_task_id: 't1',
        chain_length: 1,
        truncated: false,
      },
    },
    handoffs: emptyState(),
    events: emptyState(),
    trace: emptyState(),
    firstError: undefined,
    refreshAll: vi.fn(),
  }
}

describe('ArtifactsPanel（Phase 5A：血缘分组 / 筛选）', () => {
  it('attempt_id===null 的 legacy Artifact 单独标注分组', () => {
    render(<ArtifactsPanel bundle={bundle({ data: [LEGACY_ARTIFACT, NEW_ARTIFACT] })} />)
    expect(screen.getAllByText(/legacy · 无 Attempt 血缘/).length).toBeGreaterThan(0)
    expect(screen.getByText('seq 1')).toBeInTheDocument()
  })

  it('筛选同时作用于分组视图', () => {
    render(<ArtifactsPanel bundle={bundle({ data: [LEGACY_ARTIFACT, NEW_ARTIFACT] })} />)
    fireEvent.change(screen.getByPlaceholderText('按 type / summary / 血缘过滤'), {
      target: { value: 'new plan' },
    })
    // 只留下 a_new（plan）；legacy diff 被过滤掉
    expect(screen.getByText('new plan')).toBeInTheDocument()
    expect(screen.queryByText('legacy diff')).toBeNull()
  })
})
