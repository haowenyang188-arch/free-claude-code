import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AgentCanvasPage from './AgentCanvasPage'

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AgentCanvasPage />
    </MemoryRouter>,
  )
}

describe('AgentCanvasPage', () => {
  it('保留原生 Canvas iframe，并按会话 id 构造深链', () => {
    renderAt('/agent?c=conv-abc-123')
    const frame = screen.getByTestId('agent-canvas-frame')
    expect(frame.getAttribute('src')).toBe(
      'http://127.0.0.1:8010/conversations/conv-abc-123?backend=default-local',
    )
  })

  it('无 c 参数时显示 Canvas 首页', () => {
    renderAt('/agent')
    expect(screen.getByTestId('agent-canvas-frame').getAttribute('src')).toBe('http://127.0.0.1:8010/')
  })

  it('不渲染自建会话列表、历史面板与发送表单（原生 Canvas 内完成）', () => {
    renderAt('/agent?c=conv-abc-123')
    expect(screen.queryByText('会话列表')).not.toBeInTheDocument()
    expect(screen.queryByText('选择一个会话')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('发送消息')).not.toBeInTheDocument()
    expect(screen.queryByText('刷新')).not.toBeInTheDocument()
  })

  it('selectedId 存在时提供在原生 Canvas 打开的外链', () => {
    renderAt('/agent?c=conv-abc-123')
    expect(screen.getByText('在原生 Canvas 打开').closest('a')?.getAttribute('href')).toBe(
      'http://127.0.0.1:8010/conversations/conv-abc-123?backend=default-local',
    )
  })

  it('提供返回 SOP 总览的入口', () => {
    renderAt('/agent')
    expect(screen.getByText('← SOP 总览').closest('a')?.getAttribute('href')).toBe('/')
  })
})
