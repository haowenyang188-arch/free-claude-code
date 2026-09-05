import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { Agent, Task, Event } from './types'
import { api } from './services/api'
import { wsService } from './services/websocket'
import DashboardPage from './pages/DashboardPage'
import TaskDetailPage from './pages/TaskDetailPage'
import RunDetailPage from './pages/RunDetailPage'
import LoginPage from './pages/LoginPage'
import SopConsolePage from './pages/SopConsolePage'
import AgentCanvasPage from './pages/AgentCanvasPage'
import CollectorPage from './pages/CollectorPage'

function App() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [events, setEvents] = useState<Event[]>([])
  const [authState, setAuthState] = useState<'loading' | 'login' | 'ready'>('loading')
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let active = true
    api.getAuthStatus()
      .then((status) => {
        if (!active) return
        setAuthState(status.required && !status.authenticated ? 'login' : 'ready')
      })
      .catch(() => active && setAuthState('login'))
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (authState !== 'ready') return
    let active = true
    const load = async () => {
      try {
        const [nextAgents, nextTasks] = await Promise.all([api.getAgents(), api.getTasks()])
        if (active) {
          setAgents(nextAgents)
          setTasks(nextTasks)
        }
      } catch {
        if (active) setAuthState('login')
      }
    }
    void load()

    wsService.connect()
    const unsubscribeConnection = wsService.onConnection(setConnected)

    const unsubscribe = wsService.onMessage((message) => {
      if (message.type === 'init') {
        setAgents(message.data.agents)
        setTasks(message.data.tasks)
      } else if (message.type === 'event') {
        setEvents((prev) => [message.data, ...prev.filter((event) => event.id !== message.data.id)].slice(0, 1000))

        void load()
      }
    })

    return () => {
      active = false
      unsubscribe()
      unsubscribeConnection()
      wsService.disconnect()
    }
  }, [authState])

  if (authState === 'loading') {
    return <main className="min-h-screen bg-slate-950 p-6 text-slate-300">连接工作台…</main>
  }

  if (authState === 'login') {
    return <LoginPage onAuthenticated={() => setAuthState('ready')} />
  }

  return (
    <BrowserRouter
      future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}
    >
      <Routes>
        {/* SOP 控制台作为主入口；旧版仪表盘保留在 /dashboard */}
        <Route path="/" element={<SopConsolePage />} />
        <Route path="/sop" element={<SopConsolePage />} />
        {/* P1-A：Agent Canvas 会话视图（A 生产架构的「会话面」） */}
        <Route path="/agent" element={<AgentCanvasPage />} />
        <Route path="/collector" element={<CollectorPage />} />
        <Route path="/dashboard" element={<DashboardPage agents={agents} tasks={tasks} events={events} connected={connected} />} />
        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
        <Route path="/runs/:runId" element={<RunDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
