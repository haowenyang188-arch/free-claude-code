import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ApprovalIdentity, Run, Event, EventType, RunStatus } from '../types'
import { api } from '../services/api'

export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [run, setRun] = useState<(Omit<Run, 'events'> & { events: Event[] }) | null>(null)
  const [message, setMessage] = useState('')
  const [activeTab, setActiveTab] = useState<'messages' | 'tools' | 'files' | 'terminal'>('messages')
  const [approvalBusy, setApprovalBusy] = useState(false)

  useEffect(() => {
    if (runId) {
      loadRun()
      const interval = setInterval(loadRun, 2000)
      return () => clearInterval(interval)
    }
  }, [runId])

  const loadRun = async () => {
    if (runId) {
      const data = await api.getRun(runId)
      setRun(data)
    }
  }

  const handleSendMessage = async () => {
    if (!runId || !message.trim()) return

    await api.sendMessage(runId, message)
    setMessage('')
  }

  const handleControl = async (action: 'pause' | 'resume' | 'cancel' | 'retry') => {
    if (!runId) return

    await api.controlRun(runId, action)
    setTimeout(loadRun, 500)
  }

  const handleApproval = async (
    approval: ApprovalIdentity,
    decision: 'approve' | 'reject',
  ) => {
    if (approvalBusy) return
    setApprovalBusy(true)
    try {
      await api.decideApproval(
        approval,
        decision,
      )
      await loadRun()
    } finally {
      setApprovalBusy(false)
    }
  }

  if (!run) {
    return (
      <div className="min-h-screen bg-gray-900 text-white flex items-center justify-center">
        <div>加载中...</div>
      </div>
    )
  }

  const messageEvents = run.events.filter((e) =>
    [EventType.AGENT_MESSAGE, EventType.RUN_STARTED, EventType.RUN_FINISHED, EventType.RUN_FAILED].includes(e.type)
  )

  const toolEvents = run.events.filter((e) =>
    [EventType.TOOL_STARTED, EventType.TOOL_FINISHED].includes(e.type)
  )

  const fileEvents = run.events.filter((e) => e.type === EventType.FILE_CHANGED)

  const terminalEvents = run.events.filter((e) => e.type === EventType.TERMINAL_OUTPUT)
  const pendingApproval = [...run.events]
    .reverse()
    .find((event) => event.type === EventType.USER_INPUT_REQUIRED && event.data.approval)
    ?.data.approval as ApprovalIdentity | undefined

  const canControl = [RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.WAITING_HUMAN].includes(run.status)

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      <header className="bg-gray-800 border-b border-gray-700 p-4">
        <div className="container mx-auto flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <button onClick={() => navigate('/')} className="hover:text-blue-400">
              ← 返回
            </button>
            <h1 className="text-xl font-bold">Run 详情</h1>
            <span className={`px-3 py-1 rounded text-sm ${
              run.status === RunStatus.RUNNING ? 'bg-blue-600' :
              run.status === RunStatus.COMPLETED ? 'bg-green-600' :
              run.status === RunStatus.FAILED ? 'bg-red-600' :
              'bg-gray-600'
            }`}>
              {run.status}
            </span>
          </div>

          {canControl && (
            <div className="flex space-x-2">
              {run.status === RunStatus.RUNNING && (
                <button
                  onClick={() => handleControl('pause')}
                  className="bg-yellow-600 hover:bg-yellow-700 px-4 py-2 rounded text-sm"
                >
                  暂停
                </button>
              )}
              {run.status === RunStatus.PAUSED && (
                <button
                  onClick={() => handleControl('resume')}
                  className="bg-green-600 hover:bg-green-700 px-4 py-2 rounded text-sm"
                >
                  继续
                </button>
              )}
              <button
                onClick={() => handleControl('cancel')}
                className="bg-red-600 hover:bg-red-700 px-4 py-2 rounded text-sm"
              >
                终止
              </button>
            </div>
          )}

          {[RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED].includes(run.status) && (
            <button
              onClick={() => handleControl('retry')}
              className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded text-sm"
            >
              重试
            </button>
          )}
        </div>
      </header>

      <div className="container mx-auto p-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* 左侧信息栏 */}
          <div className="bg-gray-800 rounded-lg p-6 h-fit">
            <h2 className="text-lg font-semibold mb-4">运行信息</h2>
            <div className="space-y-2 text-sm">
              <div><span className="text-gray-400">Run ID:</span> {run.id.slice(0, 16)}</div>
              <div><span className="text-gray-400">Task ID:</span> {run.task_id.slice(0, 16)}</div>
              <div><span className="text-gray-400">Agent ID:</span> {run.agent_id.slice(0, 16)}</div>
              <div><span className="text-gray-400">开始时间:</span> {new Date(run.started_at).toLocaleString()}</div>
              {run.completed_at && (
                <div><span className="text-gray-400">完成时间:</span> {new Date(run.completed_at).toLocaleString()}</div>
              )}
              <div><span className="text-gray-400">Tokens:</span> {run.tokens_used}</div>
              <div><span className="text-gray-400">成本:</span> ${run.cost.toFixed(4)}</div>
            </div>

            {run.error && (
              <div className="mt-4 p-3 bg-red-900 bg-opacity-30 border border-red-600 rounded">
                <div className="text-sm font-medium text-red-400 mb-1">错误</div>
                <div className="text-sm text-red-300">{run.error}</div>
              </div>
            )}
          </div>

          {/* 右侧主内容区 */}
          <div className="lg:col-span-2">
            {/* Tab导航 */}
            <div className="bg-gray-800 rounded-t-lg border-b border-gray-700">
              <div className="flex space-x-1 p-2">
                {(['messages', 'tools', 'files', 'terminal'] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`px-4 py-2 rounded ${
                      activeTab === tab ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    {tab === 'messages' && '对话'}
                    {tab === 'tools' && '工具调用'}
                    {tab === 'files' && '文件变化'}
                    {tab === 'terminal' && '终端输出'}
                  </button>
                ))}
              </div>
            </div>

            {/* Tab内容 */}
            <div className="bg-gray-800 rounded-b-lg p-6">
              {activeTab === 'messages' && (
                <div className="space-y-3 max-h-[600px] overflow-y-auto mb-4">
                  {messageEvents.map((event) => (
                    <div key={event.id} className="bg-gray-700 rounded p-3">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs text-blue-400">{event.type}</span>
                        <span className="text-xs text-gray-500">{new Date(event.timestamp).toLocaleTimeString()}</span>
                      </div>
                      {event.message && <div className="text-sm">{event.message}</div>}
                    </div>
                  ))}
                  {run.status === RunStatus.WAITING_HUMAN &&
                    pendingApproval?.status === 'pending' && (
                      <div className="border border-yellow-600 bg-yellow-900/20 rounded p-4" role="status">
                        <div className="text-sm font-medium text-yellow-300 mb-2">需要一次性审批</div>
                        <code className="block text-xs text-gray-200 whitespace-pre-wrap break-words">{pendingApproval.normalized_command}</code>
                        <div className="text-xs text-gray-400 mt-2 break-all">
                          {pendingApproval.cwd} · {pendingApproval.requested_permission}
                        </div>
                        <div className="flex gap-2 mt-3">
                          <button
                            type="button"
                            disabled={approvalBusy}
                            onClick={() => handleApproval(pendingApproval, 'approve')}
                            className="bg-green-700 hover:bg-green-600 disabled:opacity-50 px-3 py-2 rounded text-sm"
                          >
                            允许一次
                          </button>
                          <button
                            type="button"
                            disabled={approvalBusy}
                            onClick={() => handleApproval(pendingApproval, 'reject')}
                            className="bg-red-700 hover:bg-red-600 disabled:opacity-50 px-3 py-2 rounded text-sm"
                          >
                            拒绝
                          </button>
                        </div>
                      </div>
                    )}
                </div>
              )}

              {activeTab === 'tools' && (
                <div className="space-y-3 max-h-[600px] overflow-y-auto">
                  {toolEvents.map((event) => (
                    <div key={event.id} className="bg-gray-700 rounded p-3">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs text-green-400">{event.type}</span>
                        <span className="text-xs text-gray-500">{new Date(event.timestamp).toLocaleTimeString()}</span>
                      </div>
                      <pre className="text-xs overflow-x-auto">{JSON.stringify(event.data, null, 2)}</pre>
                    </div>
                  ))}
                  {toolEvents.length === 0 && (
                    <div className="text-gray-500 text-center py-8">没有工具调用</div>
                  )}
                </div>
              )}

              {activeTab === 'files' && (
                <div className="space-y-3 max-h-[600px] overflow-y-auto">
                  {fileEvents.map((event) => (
                    <div key={event.id} className="bg-gray-700 rounded p-3">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs text-purple-400">{event.type}</span>
                        <span className="text-xs text-gray-500">{new Date(event.timestamp).toLocaleTimeString()}</span>
                      </div>
                      <pre className="text-xs overflow-x-auto">{JSON.stringify(event.data, null, 2)}</pre>
                    </div>
                  ))}
                  {fileEvents.length === 0 && (
                    <div className="text-gray-500 text-center py-8">没有文件变化</div>
                  )}
                </div>
              )}

              {activeTab === 'terminal' && (
                <div className="space-y-3 max-h-[600px] overflow-y-auto">
                  {terminalEvents.map((event) => (
                    <div key={event.id} className="bg-gray-700 rounded p-3">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs text-yellow-400">{event.type}</span>
                        <span className="text-xs text-gray-500">{new Date(event.timestamp).toLocaleTimeString()}</span>
                      </div>
                      <pre className="text-xs overflow-x-auto font-mono">{JSON.stringify(event.data, null, 2)}</pre>
                    </div>
                  ))}
                  {terminalEvents.length === 0 && (
                    <div className="text-gray-500 text-center py-8">没有终端输出</div>
                  )}
                </div>
              )}

              {/* 输入框 */}
              {run.status === RunStatus.WAITING_HUMAN && activeTab === 'messages' && (
                <div className="flex space-x-2 mt-4">
                  <input
                    type="text"
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && handleSendMessage()}
                    placeholder="输入消息..."
                    className="flex-1 bg-gray-700 rounded px-4 py-2"
                  />
                  <button
                    onClick={handleSendMessage}
                    className="bg-blue-600 hover:bg-blue-700 px-6 py-2 rounded"
                  >
                    发送
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
