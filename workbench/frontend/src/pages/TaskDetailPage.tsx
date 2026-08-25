import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Task } from '../types'
import { api } from '../services/api'

export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const [task, setTask] = useState<Task | null>(null)

  useEffect(() => {
    if (taskId) {
      api.getTask(taskId).then(setTask)
    }
  }, [taskId])

  if (!task) {
    return (
      <div className="min-h-screen bg-gray-900 text-white flex items-center justify-center">
        <div>加载中...</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      <header className="bg-gray-800 border-b border-gray-700 p-4">
        <div className="container mx-auto flex items-center space-x-4">
          <button onClick={() => navigate('/')} className="hover:text-blue-400">
            ← 返回
          </button>
          <h1 className="text-xl font-bold">{task.title}</h1>
        </div>
      </header>

      <div className="container mx-auto p-6">
        <div className="bg-gray-800 rounded-lg p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4">任务详情</h2>
          <div className="space-y-2 text-sm">
            <div><span className="text-gray-400">状态:</span> {task.status}</div>
            <div><span className="text-gray-400">描述:</span> {task.description}</div>
            <div><span className="text-gray-400">工作空间:</span> {task.workspace_path}</div>
            <div><span className="text-gray-400">创建时间:</span> {new Date(task.created_at).toLocaleString()}</div>
          </div>
        </div>

        <div className="bg-gray-800 rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4">运行历史</h2>
          <div className="space-y-2">
            {task.runs.map((runId, index) => (
              <div
                key={runId}
                onClick={() => navigate(`/runs/${runId}`)}
                className="bg-gray-700 rounded p-4 cursor-pointer hover:bg-gray-600"
              >
                <div className="flex items-center justify-between">
                  <span>Run #{index + 1}</span>
                  <span className="text-sm text-gray-400">{runId.slice(0, 8)}</span>
                </div>
              </div>
            ))}
            {task.runs.length === 0 && (
              <div className="text-gray-500 text-center py-4">没有运行记录</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
