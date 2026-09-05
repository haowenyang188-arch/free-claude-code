import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Agent, Task, Event, AgentType, AgentStatus, TaskStatus, CreateTaskRequest } from '../types'
import { api } from '../services/api'

interface Props {
  agents: Agent[]
  tasks: Task[]
  events: Event[]
  connected: boolean
}

export default function DashboardPage({ agents, tasks, events, connected }: Props) {
  const navigate = useNavigate()
  const [showCreateTask, setShowCreateTask] = useState(false)
  const [newTask, setNewTask] = useState<CreateTaskRequest>({
    title: '',
    description: '',
    agent_type: AgentType.CODEX,
  })
  const [error, setError] = useState('')

  const handleCreateTask = async () => {
    try {
      setError('')
      const task = await api.createTask(newTask)
      setShowCreateTask(false)
      setNewTask({ title: '', description: '', agent_type: AgentType.CODEX })

      // 立即启动任务
      const run = await api.startTask(task.id)
      navigate(`/runs/${run.id}`)
    } catch (error) {
      setError(error instanceof Error ? error.message : '创建任务失败')
    }
  }

  const getStatusColor = (status: AgentStatus | TaskStatus): string => {
    switch (status) {
      case AgentStatus.ONLINE:
      case TaskStatus.COMPLETED:
        return 'bg-green-500'
      case AgentStatus.BUSY:
      case TaskStatus.RUNNING:
        return 'bg-blue-500'
      case AgentStatus.ERROR:
      case TaskStatus.FAILED:
        return 'bg-red-500'
      case AgentStatus.OFFLINE:
      case TaskStatus.PENDING:
        return 'bg-gray-500'
      case TaskStatus.PAUSED:
      case TaskStatus.WAITING_HUMAN:
        return 'bg-yellow-500'
      default:
        return 'bg-gray-500'
    }
  }

  const getAgentTypeName = (type: AgentType): string => {
    switch (type) {
      case AgentType.CODEX:
        return 'Codex'
      case AgentType.CLAUDE_CODE:
        return 'Claude Code'
      case AgentType.OPENHANDS:
        return 'OpenHands'
      default:
        return type
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* 顶部导航 */}
      <header className="border-b border-slate-800 bg-slate-900/95 px-4 py-4 backdrop-blur sm:px-6">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => navigate('/')}
              className="rounded-md border border-slate-700 px-3 py-1 text-sm text-slate-300 hover:bg-slate-800 hover:text-white"
              aria-label="返回 SOP 控制台"
            >
              ← 返回
            </button>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">WSL Control Plane</p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight sm:text-2xl">智能体工作台</h1>
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-400" role="status" aria-live="polite">
            <span className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-amber-400'}`} aria-hidden="true" />
            {connected ? '实时连接' : '正在重连'}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-4 p-4 sm:space-y-6 sm:p-6">
        {error && <p role="alert" className="rounded-lg border border-rose-900/70 bg-rose-950/40 px-4 py-3 text-sm text-rose-300">{error}</p>}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
          {/* Agent列表 */}
          <section className="rounded-xl border border-slate-800 bg-slate-900 p-4 sm:p-6" aria-labelledby="agents-heading">
            <div className="mb-4 flex items-center justify-between">
              <h2 id="agents-heading" className="text-lg font-semibold sm:text-xl">Agent 列表</h2>
              <span className="text-xs text-slate-500">{agents.length} 个</span>
            </div>
            <div className="space-y-3">
              {agents.map((agent) => (
                <div key={agent.id} className="rounded-lg border border-slate-800 bg-slate-800/70 p-4">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium">{getAgentTypeName(agent.type)}</span>
                    <span className="flex items-center gap-2 text-xs text-slate-300">
                      <span className={`h-2 w-2 rounded-full ${getStatusColor(agent.status)}`} aria-hidden="true" />
                      {agent.status}
                    </span>
                  </div>
                  <div className="text-sm text-gray-400">
                    <div>ID: {agent.id.slice(0, 8)}</div>
                    {agent.current_task_id && (
                      <div className="text-blue-400">运行中</div>
                    )}
                  </div>
                </div>
              ))}
              {agents.length === 0 && (
                <div className="py-4 text-center text-sm text-slate-500">没有可用的 Agent</div>
              )}
            </div>
          </section>

          {/* 任务列表 */}
          <section className="rounded-xl border border-slate-800 bg-slate-900 p-4 sm:p-6 lg:col-span-2" aria-labelledby="tasks-heading">
            <div className="flex items-center justify-between mb-4">
              <h2 id="tasks-heading" className="text-lg font-semibold sm:text-xl">任务列表</h2>
              <button
                type="button"
                onClick={() => setShowCreateTask(true)}
                className="rounded-lg bg-cyan-400 px-3 py-2 text-sm font-semibold text-slate-950 transition hover:bg-cyan-300"
              >
                创建任务
              </button>
            </div>

            <div className="space-y-3">
              {tasks.map((task) => (
                <button
                  key={task.id}
                  onClick={() => navigate(`/tasks/${task.id}`)}
                  type="button"
                  className="w-full rounded-lg border border-slate-800 bg-slate-800/70 p-4 text-left transition hover:border-slate-600 hover:bg-slate-800"
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium">{task.title}</span>
                    <span className="flex items-center gap-2 text-xs text-slate-300">
                      <span className={`h-2 w-2 rounded-full ${getStatusColor(task.status)}`} aria-hidden="true" />
                      {task.status}
                    </span>
                  </div>
                  <div className="text-sm text-gray-400 mb-2">{task.description}</div>
                  <div className="flex items-center justify-between text-xs text-gray-500">
                    <span>{task.agent_type && getAgentTypeName(task.agent_type)}</span>
                    <span>{task.runs.length} 次运行</span>
                  </div>
                </button>
              ))}
              {tasks.length === 0 && (
                <div className="py-8 text-center text-sm text-slate-500">
                  暂无任务，创建一个手机可控的 Codex 或 Claude 运行
                </div>
              )}
            </div>
          </section>
        </div>

        {/* 实时事件流 */}
        <section className="rounded-xl border border-slate-800 bg-slate-900 p-4 sm:p-6" aria-labelledby="events-heading">
          <div className="mb-4 flex items-center justify-between">
            <h2 id="events-heading" className="text-lg font-semibold sm:text-xl">实时事件</h2>
            <span className="text-xs text-slate-500">最近 {events.length} 条</span>
          </div>
          <div className="max-h-96 space-y-2 overflow-y-auto">
            {events.map((event) => (
              <div key={event.id} className="rounded-lg border border-slate-800 bg-slate-800/60 p-3 text-sm">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-mono text-cyan-300">{event.type}</span>
                  <span className="text-slate-500">{new Date(event.timestamp).toLocaleTimeString()}</span>
                </div>
                {event.message && <div className="text-slate-300">{event.message}</div>}
              </div>
            ))}
            {events.length === 0 && (
              <div className="py-4 text-center text-sm text-slate-500">暂无事件</div>
            )}
          </div>
        </section>
      </main>

      {/* 创建任务对话框 */}
      {showCreateTask && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4">
          <div role="dialog" aria-modal="true" aria-labelledby="create-task-title" className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl sm:p-6">
            <h3 id="create-task-title" className="mb-4 text-xl font-semibold">创建新任务</h3>

            <div className="space-y-4">
              <div>
                  <label htmlFor="task-title" className="mb-2 block text-sm font-medium">任务标题</label>
                  <input
                    id="task-title"
                  type="text"
                  value={newTask.title}
                  onChange={(e) => setNewTask({ ...newTask, title: e.target.value })}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-3 text-sm outline-none focus:border-cyan-400"
                  placeholder="例如：优化项目性能"
                />
              </div>

              <div>
                  <label htmlFor="task-description" className="mb-2 block text-sm font-medium">任务描述</label>
                  <textarea
                    id="task-description"
                  value={newTask.description}
                  onChange={(e) => setNewTask({ ...newTask, description: e.target.value })}
                  className="h-32 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-3 text-sm outline-none focus:border-cyan-400"
                  placeholder="详细描述任务要求..."
                />
              </div>

              <div>
                  <label htmlFor="agent-type" className="mb-2 block text-sm font-medium">选择 Agent</label>
                  <select
                    id="agent-type"
                  value={newTask.agent_type}
                  onChange={(e) => setNewTask({ ...newTask, agent_type: e.target.value as AgentType })}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-3 text-sm outline-none focus:border-cyan-400"
                >
                  <option value={AgentType.CODEX}>Codex</option>
                  <option value={AgentType.CLAUDE_CODE}>Claude Code</option>
                </select>
              </div>
            </div>

            <div className="flex justify-end space-x-3 mt-6">
              <button
                type="button"
                onClick={() => setShowCreateTask(false)}
                className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleCreateTask}
                disabled={!newTask.title || !newTask.description}
                className="rounded-lg bg-cyan-400 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-50"
              >
                创建并启动
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
