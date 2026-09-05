import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CreateSopRunForm } from './CreateSopRunForm'

const { startSopRun } = vi.hoisted(() => ({ startSopRun: vi.fn() }))
vi.mock('../../services/sopApi', () => ({ startSopRun }))

describe('CreateSopRunForm', () => {
  it('开启真实的自动执行请求，不停留在仅创建状态', async () => {
    startSopRun.mockResolvedValue({
      sop_run_id: 'run-1',
      goal_id: 'goal-1',
      status: 'running',
      started_at: '2026-09-04T11:00:00Z',
    })
    render(
      <CreateSopRunForm
        definitions={[{ id: 'def-1', name: '测试定义', version: 1, description: null, step_count: 1, stages: [], steps: ['plan'] }]}
        definitionsError={undefined}
        onReloadDefinitions={vi.fn()}
        onCreated={vi.fn()}
        onSelect={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByPlaceholderText('让 SOP 引擎下发的目标'), { target: { value: '执行测试' } })
    fireEvent.click(screen.getByRole('button', { name: '创建并下发' }))

    await waitFor(() => expect(startSopRun).toHaveBeenCalledWith(expect.objectContaining({
      sop_definition_id: 'def-1',
      goal_description: '执行测试',
      auto_execute: true,
    })))
  })
})
