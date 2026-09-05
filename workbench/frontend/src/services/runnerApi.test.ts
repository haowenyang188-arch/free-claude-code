import { afterEach, describe, expect, it, vi } from 'vitest'
import { RUNNER_BASE, runnerApi } from './runnerApi'

afterEach(() => vi.restoreAllMocks())

describe('runnerApi', () => {
  it('sends only the allowlisted start body to the local runner', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ run_id: 'abc12345', status: 'running', pid: 99 }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await runnerApi.start({
      channel: 'xhs',
      boot: true,
      limit: 20,
      restart: false,
      smoke: false,
      kill_chrome: false,
    })

    expect(fetchMock).toHaveBeenCalledWith(
      `${RUNNER_BASE}/runs/start`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          channel: 'xhs',
          boot: true,
          limit: 20,
          restart: false,
          smoke: false,
          kill_chrome: false,
        }),
      }),
    )
  })

  it('surfaces structured runner errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { code: 'CDP_UNAVAILABLE', message: 'Chrome 未就绪' } }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(runnerApi.start({
      channel: 'douyin',
      boot: false,
      limit: 1,
      restart: false,
      smoke: true,
      kill_chrome: false,
    })).rejects.toMatchObject({ status: 503, code: 'CDP_UNAVAILABLE', message: 'Chrome 未就绪' })
  })

  it('normalizes the existing HTTPS local_runner envelope and channel names', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/health')) {
        return new Response(JSON.stringify({ ok: true, service: 'local_runner', tasks: 2 }), { status: 200 })
      }
      if (url.endsWith('/tasks')) {
        return new Response(JSON.stringify({ ok: true, tasks: [
          { channel: '小红书-9222' },
          { channel: '抖音-9224' },
        ] }), { status: 200 })
      }
      if (url.endsWith('/runs/start')) {
        expect(JSON.parse(String(init?.body))).toMatchObject({ channel: '小红书-9222' })
        return new Response(JSON.stringify({ ok: true, run: {
          id: 'legacy01', channel: '小红书-9222', status: 'running', pid: 321,
          started_at: '2026-09-05 10:00:00', out: 'data/result.json', boot_ok: true,
          cmd: 'wsl.exe ...',
        } }), { status: 200 })
      }
      if (url.includes('/runs/legacy01/logs?offset=0')) {
        return new Response(JSON.stringify({ ok: true, id: 'legacy01', next_offset: 2, done: false, lines: ['a', 'b'] }), { status: 200 })
      }
      if (url.endsWith('/runs/legacy01/import')) {
        return new Response(JSON.stringify({ ok: true, run: {
          id: 'legacy01', channel: '小红书-9222', status: 'exited', pid: 321,
          exit_code: 0, imported: true,
        }, output_file: '/home/gnen/scraper/data/result.json', record_count: 2, sha256: 'abc123' }), { status: 200 })
      }
      throw new Error(`unexpected URL ${url}`)
    })

    await expect(runnerApi.health()).resolves.toMatchObject({ channels: ['xhs', 'douyin'] })
    await expect(runnerApi.start({
      channel: 'xhs', boot: true, limit: 2, restart: false, smoke: false, kill_chrome: false,
    })).resolves.toMatchObject({
      run_id: 'legacy01', channel: 'xhs', status: 'running', pid: 321,
      output_files: ['data/result.json'], chrome_booted: true,
    })
    await expect(runnerApi.getLogs('legacy01')).resolves.toEqual({
      run_id: 'legacy01', lines: ['a', 'b'], next: 2, done: false,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      `${RUNNER_BASE}/runs/legacy01/logs?offset=0`,
      expect.anything(),
    )
    await expect(runnerApi.importOutput('legacy01')).resolves.toMatchObject({
      output_file: '/home/gnen/scraper/data/result.json', record_count: 2, sha256: 'abc123',
    })
  })

  it('rejects a successful HTTP response that reports ok=false', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: false, error: 'channel already running' }), { status: 200 }),
    )

    await expect(runnerApi.health()).rejects.toMatchObject({
      status: 200,
      message: 'channel already running',
    })
  })
})
