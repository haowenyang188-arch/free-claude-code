import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { UsageGuidePanel } from './UsageGuidePanel'

describe('UsageGuidePanel', () => {
  it('展示固定协作顺序与角色边界', () => {
    render(<UsageGuidePanel />)

    fireEvent.click(screen.getByRole('button', { name: '打开协作与工具指南' }))

    expect(screen.getByRole('heading', { name: '怎么协作' })).toBeInTheDocument()
    expect(screen.getByTestId('usage-guide-collaboration-order')).toHaveTextContent(
      'Claude 方案 → Codex 审核 → Claude 修订 → Codex 复审 → Codex 输出 → 用户决策 → Codex 执行',
    )
    expect(screen.getByText('SOP Engine')).toBeInTheDocument()
    expect(screen.getByText('Artifact + Handoff')).toBeInTheDocument()
    expect(screen.getByText('Codex')).toBeInTheDocument()
    expect(screen.getByText(/审核 \/ 复审 \/ 最终门/)).toBeInTheDocument()
  })

  it('展示工具用途、审批前置条件和重复下发提醒', () => {
    render(<UsageGuidePanel />)

    fireEvent.click(screen.getByRole('button', { name: '打开协作与工具指南' }))

    expect(screen.getByRole('heading', { name: '工具操作速查' })).toBeInTheDocument()
    expect(screen.getByTestId('usage-guide-tools')).toHaveTextContent('下发')
    expect(screen.getByTestId('usage-guide-tools')).toHaveTextContent('流程')
    expect(screen.getByText(/session_id.*call_id.*64 位 command_hash/)).toBeInTheDocument()
    expect(screen.getByText(/不要重复下发同一 run/)).toBeInTheDocument()
  })

  it('默认只显示图标，并支持键盘打开和关闭', () => {
    render(<UsageGuidePanel />)

    const launcher = screen.getByRole('button', { name: '打开协作与工具指南' })
    expect(launcher).toHaveAttribute('title', '打开协作与工具指南')
    expect(screen.queryByRole('heading', { name: '怎么协作' })).not.toBeInTheDocument()

    launcher.focus()
    fireEvent.keyDown(launcher, { key: 'Enter' })
    expect(screen.getByRole('heading', { name: '怎么协作' })).toBeInTheDocument()

    const close = screen.getByRole('button', { name: '关闭协作与工具指南' })
    close.focus()
    fireEvent.keyDown(close, { key: ' ' })
    expect(screen.queryByRole('heading', { name: '怎么协作' })).not.toBeInTheDocument()
  })

  it('拖动指南头部会更新面板位置', () => {
    render(<UsageGuidePanel />)
    fireEvent.click(screen.getByRole('button', { name: '打开协作与工具指南' }))

    const panel = screen.getByRole('dialog', { name: '协作与工具指南' })
    const handle = screen.getByRole('button', { name: '拖动协作与工具指南' })
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 140, clientY: 130 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 140, clientY: 130 })

    expect(panel).toHaveStyle({ transform: 'translate3d(40px, 30px, 0)' })
  })
})
