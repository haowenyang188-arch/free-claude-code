import { Agent, Task, Run, Event, CreateTaskRequest, ApprovalIdentity, ApprovalRecord } from '../types'

const API_BASE = '/api'

/** 统一的 /api 请求封装。SOP 控制台（services/sopApi.ts）复用它，不另起一套。 */
export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const error = new Error(detail?.error?.message || detail?.detail || `Request failed (${response.status})`)
    Object.assign(error, { status: response.status })
    throw error
  }
  return response.json() as Promise<T>
}

export const api = {
  async getAuthStatus(): Promise<{ authenticated: boolean; required: boolean }> {
    return requestJson('/auth/status')
  },

  async login(token: string): Promise<void> {
    await requestJson('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ token }),
    })
  },

  async logout(): Promise<void> {
    await requestJson('/auth/logout', { method: 'POST' })
  },

  async getAgents(): Promise<Agent[]> {
    const data = await requestJson<{ agents: Agent[] }>('/agents')
    return data.agents
  },

  async getTasks(): Promise<Task[]> {
    const data = await requestJson<{ tasks: Task[] }>('/tasks')
    return data.tasks
  },

  async getTask(taskId: string): Promise<Task> {
    return requestJson(`/tasks/${taskId}`)
  },

  async createTask(request: CreateTaskRequest): Promise<Task> {
    return requestJson('/tasks', {
      method: 'POST',
      body: JSON.stringify(request),
    })
  },

  async startTask(taskId: string): Promise<Run> {
    return requestJson(`/tasks/${taskId}/start`, {
      method: 'POST',
    })
  },

  async getRun(runId: string): Promise<Omit<Run, 'events'> & { events: Event[] }> {
    return requestJson(`/runs/${runId}`)
  },

  async sendMessage(runId: string, message: string): Promise<{ success: boolean }> {
    return requestJson(`/runs/${runId}/message`, {
      method: 'POST',
      body: JSON.stringify({ run_id: runId, message }),
    })
  },

  async controlRun(runId: string, action: 'pause' | 'resume' | 'cancel' | 'retry'): Promise<{ success: boolean }> {
    return requestJson(`/runs/${runId}/control`, {
      method: 'POST',
      body: JSON.stringify({ run_id: runId, action }),
    })
  },

  async decideApproval(
    approval: ApprovalIdentity,
    decision: 'approve' | 'reject' | 'cancel',
  ): Promise<ApprovalRecord> {
    return requestJson(
      `/approvals/${encodeURIComponent(approval.session_id)}/${encodeURIComponent(approval.call_id)}/${decision}`,
      {
        method: 'POST',
        body: JSON.stringify({
          provider: approval.provider,
          command_hash: approval.command_hash,
          one_shot_id: approval.one_shot_id,
          thread_id: approval.thread_id,
          turn_id: approval.turn_id,
          item_id: approval.item_id,
          approval_id: approval.approval_id,
        }),
      },
    )
  },
}
