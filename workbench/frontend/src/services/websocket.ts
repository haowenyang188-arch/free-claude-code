import { Agent, Task, Event } from '../types'

type WebSocketMessage =
  | { type: 'init'; data: { agents: Agent[]; tasks: Task[] } }
  | { type: 'event'; data: Event }

type MessageHandler = (message: WebSocketMessage) => void
type ConnectionHandler = (connected: boolean) => void

export class WebSocketService {
  private ws: WebSocket | null = null
  private handlers: MessageHandler[] = []
  private reconnectInterval = 3000
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private connectionHandlers: ConnectionHandler[] = []

  connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws`

    this.ws = new WebSocket(wsUrl)

    this.ws.onopen = () => {
      this.connectionHandlers.forEach((handler) => handler(true))
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
    }

    this.ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data)
        this.handlers.forEach((handler) => handler(message))
      } catch (error) {
        console.error('WebSocket message parse error:', error)
      }
    }

    this.ws.onerror = (error) => {
      void error
      this.connectionHandlers.forEach((handler) => handler(false))
    }

    this.ws.onclose = () => {
      this.connectionHandlers.forEach((handler) => handler(false))
      this.reconnectTimer = setTimeout(() => this.connect(), this.reconnectInterval)
    }
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.connectionHandlers.forEach((handler) => handler(false))
  }

  onMessage(handler: MessageHandler) {
    this.handlers.push(handler)
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler)
    }
  }

  onConnection(handler: ConnectionHandler) {
    this.connectionHandlers.push(handler)
    return () => {
      this.connectionHandlers = this.connectionHandlers.filter((item) => item !== handler)
    }
  }

  send(data: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data))
    }
  }
}

export const wsService = new WebSocketService()
