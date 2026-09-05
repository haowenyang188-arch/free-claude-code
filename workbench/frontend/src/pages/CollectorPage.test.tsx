import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CollectorPage from './CollectorPage'

const runnerApiMock = vi.hoisted(() => ({
  health: vi.fn(),
  start: vi.fn(),
  getRun: vi.fn(),
  getLogs: vi.fn(),
  stop: vi.fn(),
  register: vi.fn(),
  importOutput: vi.fn(),
}))

vi.mock('../services/runnerApi', () => ({ runnerApi: runnerApiMock }))

const runningRun = {
  run_id: 'abc12345',
  channel: 'xhs' as const,
  status: 'running' as const,
  pid: 21520,
  started_at: '2026-09-04T11:00:00Z',
  finished_at: null,
  exit_code: null,
  argv: ['python3', '/home/gnen/scraper/main.py'],
  output_files: [],
  registered: false,
  imported: false,
  chrome_booted: true,
  chrome_cleanup_error: null,
}

describe('CollectorPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    runnerApiMock.health.mockResolvedValue({
      status: 'ok',
      runner_version: '1.0.0',
      channels: ['xhs', 'douyin'],
      scraper_root: '/home/gnen/scraper',
    })
    runnerApiMock.getRun.mockResolvedValue(runningRun)
    runnerApiMock.getLogs.mockResolvedValue({ run_id: 'abc12345', lines: ['采集开始'], next: 1, done: false })
    runnerApiMock.start.mockResolvedValue(runningRun)
    runnerApiMock.stop.mockResolvedValue({ ...runningRun, status: 'stopped', exit_code: -15 })
  })

  it('starts a real runner and then stops the returned run', async () => {
    render(
      <MemoryRouter>
        <CollectorPage />
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('执行层已连接')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '开始采集' }))

    await waitFor(() => expect(runnerApiMock.start).toHaveBeenCalledWith({
      channel: 'xhs',
      boot: true,
      limit: 20,
      restart: false,
      smoke: false,
      kill_chrome: false,
    }))
    expect(await screen.findByText('PID 21520')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '停止采集' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '停止采集' }))
    await waitFor(() => expect(runnerApiMock.stop).toHaveBeenCalledWith('abc12345', false))
  })
})
