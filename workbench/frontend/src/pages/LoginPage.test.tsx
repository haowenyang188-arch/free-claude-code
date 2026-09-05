import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import LoginPage from './LoginPage'

vi.mock('../services/api', () => ({
  api: {
    login: vi.fn(),
  },
}))

describe('LoginPage', () => {
  it('explains where the Workbench token comes from and distinguishes it from Agent Canvas auth', () => {
    render(<LoginPage onAuthenticated={vi.fn()} />)

    expect(screen.getByText(/Workbench token/i)).toBeInTheDocument()
    expect(screen.getByText(/启动脚本会在用户运行时目录生成/i)).toBeInTheDocument()
    expect(screen.getByText(/cd \/home\/gnen\/free-claude-code/i)).toBeInTheDocument()
    expect(screen.getByText(/不是 Agent Canvas 的会话密钥/i)).toBeInTheDocument()
    expect(screen.getByText(/workbench\/launcher\.sh status/i)).toBeInTheDocument()
  })
})
