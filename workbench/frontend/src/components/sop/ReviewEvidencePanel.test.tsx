import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ResourceState } from '../../hooks/useResource'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { ReviewEvidencePanel } from './ReviewEvidencePanel'
import type { ReviewEvidenceResponse } from '../../types/sop'

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

function evidenceState(overrides: Partial<ResourceState<ReviewEvidenceResponse[]>>) {
  return { ...emptyState<ReviewEvidenceResponse[]>(), ...overrides }
}

function bundle(state: ResourceState<ReviewEvidenceResponse[]>): SopRunBundle {
  return {
    runId: 'run-1',
    enabled: true,
    run: emptyState(),
    steps: emptyState(),
    tasks: { ...emptyState(), data: [] },
    artifacts: emptyState(),
    attempts: emptyState(),
    evidence: state,
    chain: emptyState(),
    handoffs: emptyState(),
    events: emptyState(),
    trace: emptyState(),
    firstError: undefined,
    refreshAll: vi.fn(),
  }
}

const VALID_ITEM: ReviewEvidenceResponse = {
  review_id: 'r1',
  reviewed_attempt_id: 'att_exec',
  review_status: 'approved',
  outcome: 'PASS',
  blocking_items: [],
  outcome_parse_error: null,
  execution_diff: {
    id: 'a_diff', task_id: 't_exec', type: 'diff', uri: null, summary: 'diff',
    sha256: 'x', accepted: true, created_at: '2026-08-31T00:00:00Z',
    attempt_id: 'att_exec', producer_step_run_id: 'sr1', role_id: 'dsh',
  },
  execution_test_report: {
    id: 'a_test', task_id: 't_exec', type: 'test_report', uri: null, summary: 'test',
    sha256: 'x', accepted: true, created_at: '2026-08-31T00:00:00Z',
    attempt_id: 'att_exec', producer_step_run_id: 'sr1', role_id: 'dsh',
  },
  reviewer_report: {
    id: 'a_rev', task_id: 't_rev', type: 'review_report', uri: null, summary: 'review',
    sha256: 'x', accepted: true, created_at: '2026-08-31T00:00:00Z',
    attempt_id: 'att_rev', producer_step_run_id: 'sr2', role_id: 'codex',
  },
  reviewer_attempt_id: 'att_rev',
  evidence_valid: true,
  evidence_complete: true,
  created_at: '2026-08-31T00:00:00Z',
}

describe('ReviewEvidencePanel', () => {
  it('loading 态：显示加载中', () => {
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ loading: true }))} />)
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('empty 态：显示暂无评审证据', () => {
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ data: [] }))} />)
    expect(screen.getByText('该 run 暂无评审证据（没有 Review 记录）。')).toBeInTheDocument()
  })

  it('error 态：非阻塞 ErrorNotice', () => {
    render(
      <ReviewEvidencePanel
        bundle={bundle(evidenceState({ error: new Error('backend down') }))}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent(/读取评审证据失败/)
  })

  it('无效 Evidence 不得显示绿色：outcome=PASS 但 valid=false → 警示而非成功', () => {
    const invalid = {
      ...VALID_ITEM,
      outcome: 'PASS',
      evidence_valid: false,
      evidence_complete: true,
    }
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ data: [invalid] }))} />)
    // 不出现成功徽标
    expect(screen.queryByText('evidence valid')).toBeNull()
    expect(screen.queryByText('PASS')).not.toBeNull()
    // 显式警示：PASS + 无效证据必须有不依赖颜色的文字（绝不显示成合法绿色 PASS）
    expect(screen.getByText('（无效证据）')).toBeInTheDocument()
    expect(screen.getByText(/证据三件套齐全但无效/)).toBeInTheDocument()
  })

  it('无效证据 + review_status=approved：状态徽标不得使用 success 绿色', () => {
    const invalid = {
      ...VALID_ITEM,
      review_status: 'approved',
      evidence_valid: false,
      evidence_complete: false,
    }
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ data: [invalid] }))} />)
    const badge = screen.getByText('approved').closest('span')
    // P0：labels 已迁移到语义 state-* token；断言语义层而非字面色名。
    expect(badge?.className ?? '').not.toContain('state-success')
    expect(badge?.className ?? '').toContain('state-warn')
  })

  it('legacy 记录（reviewed_attempt_id=null）单独标注，不显示绿色', () => {
    const legacy = {
      ...VALID_ITEM,
      reviewed_attempt_id: null,
      reviewer_attempt_id: null,
      evidence_valid: false,
      evidence_complete: false,
    }
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ data: [legacy] }))} />)
    expect(screen.getByText(/legacy · 无 Attempt 血缘/)).toBeInTheDocument()
    expect(screen.queryByText('evidence valid')).toBeNull()
  })

  it('合法证据显示 evidence valid 成功徽标', () => {
    render(<ReviewEvidencePanel bundle={bundle(evidenceState({ data: [VALID_ITEM] }))} />)
    expect(screen.getByText('evidence valid')).toBeInTheDocument()
    expect(screen.queryByText(/legacy/)).toBeNull()
  })
})
