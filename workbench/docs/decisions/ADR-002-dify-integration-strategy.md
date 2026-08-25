# ADR-002: Dify Integration Strategy

**Status**: Proposed  
**Date**: 2026-08-25  
**Context**: Phase 2 (Frontend API) 实现前的架构决策

## 背景

经过调研，GitHub 上不存在同时满足以下要求的成熟产品：
1. 可视化 Workflow 画布
2. 原生支持 Claude Code / Codex CLI Runtime
3. Role → Capability → Agent → Runtime 架构
4. Artifact-only handoff + 上下文隔离

最接近的成熟产品是 **Dify** (15万 Stars)，它提供：
- ✅ 完整可视化 Workflow 画布
- ✅ Human Input / 人工暂停
- ✅ Workflow Run 历史和节点输出查看
- ✅ 文件和变量传递
- ✅ 可部署平台 (Docker)
- ❌ 但不原生支持 Claude Code/Codex CLI Runtime

## 决策

**不继续开发自有前端 UI**，而是：

```
Dify Workflow (可视化层)
    ↓ HTTP/MCP
SOP Orchestrator Bridge (本项目)
    ↓ SubagentRunner
Claude Code / Codex CLI Runtime
```

## 架构调整

### 当前已完成组件 (保留)

```
workbench/backend/
├── domain/models.py           ✅ 保留 (SOP/Step/Task/Artifact/Handoff)
├── persistence/sqlite_store.py ✅ 保留 (状态持久化)
├── workflow/engine.py         ✅ 保留 (核心编排引擎)
├── workflow/runners.py        ✅ 保留 (Runtime 抽象层)
├── workflow/adapters/
│   └── claude_code.py         ✅ 保留 (Claude Code 适配器)
├── artifacts/store.py         ✅ 保留 (Artifact 存储)
└── runtime/events.py          ✅ 保留 (事件日志)
```

### 新增组件 (Bridge Layer)

```
workbench/backend/
├── bridge/
│   ├── dify_protocol.py       # Dify Workflow API 协议适配
│   ├── mcp_server.py          # MCP Server 实现 (可选)
│   └── translator.py          # Dify Node → SOP Step 转换
└── api/
    ├── bridge_routes.py       # Bridge HTTP API
    └── webhook.py             # Dify Webhook 回调
```

## 接口设计

### Dify → SOP Bridge

Dify 通过 HTTP Tool 或 Custom Node 调用：

```http
POST /api/bridge/execute_step
Content-Type: application/json

{
  "workflow_run_id": "dify-run-123",
  "node_id": "researcher-node",
  "role": "security_researcher",
  "capability": "research_oauth2",
  "goal": "Research OAuth2 best practices",
  "instructions": "Focus on PKCE and token storage",
  "constraints": ["max 1000 tokens", "use only public sources"],
  "upstream_artifacts": [
    {"id": "prev-node-output", "content": "..."}
  ],
  "runtime_kind": "claude_code",
  "workspace_scope": "/project/auth"
}

Response:
{
  "task_id": "task-456",
  "status": "running",
  "polling_url": "/api/bridge/tasks/task-456"
}
```

### 轮询或 Webhook

```http
GET /api/bridge/tasks/task-456

Response:
{
  "task_id": "task-456",
  "status": "completed",
  "artifact": {
    "id": "artifact-789",
    "type": "text",
    "content": "OAuth2 recommendations:\n1. Use PKCE\n2. ...",
    "summary": "OAuth2 research complete"
  }
}
```

或 Dify 注册 Webhook：

```http
POST {dify_webhook_url}
{
  "task_id": "task-456",
  "status": "completed",
  "artifact": {...}
}
```

## 实现优先级

### Phase 2-Revised: Dify Bridge MVP

1. **Bridge API** (2-3 天)
   - `POST /api/bridge/execute_step` - 接收 Dify 节点执行请求
   - `GET /api/bridge/tasks/{task_id}` - 轮询任务状态
   - `POST /api/bridge/webhooks/{workflow_id}` - Webhook 回调注册

2. **Dify Protocol Adapter** (1-2 天)
   - Dify Workflow Variables → SOP Context 映射
   - Dify Node Output → Artifact 转换
   - Error handling 和重试逻辑

3. **Claude Code Runtime Integration** (已完成)
   - RuntimeNeutralRunner ✅
   - ClaudeCodeAdapter ✅

4. **Documentation** (1 天)
   - Dify Custom Node 配置示例
   - Workflow Template (研究 → 实现 → 审核)

### Phase 3: 增强功能

- Codex Adapter 实现
- Human Review Gate (Dify Human Input 集成)
- Artifact Store HTTP API (下载中间产物)
- EventLog WebSocket 订阅 (实时日志)

## 成本收益

### 如果继续开发自有 UI

- 成本：前端开发 4-6 周 + 持续维护
- 收益：完全定制化

### 如果采用 Dify + Bridge

- 成本：Bridge 开发 1 周 + Dify 学习
- 收益：立即获得成熟可视化工作台、社区支持、持续更新

**决策：采用 Dify + Bridge 方案**

## 后果

### 正面

- 立即获得生产级可视化 Workflow 平台
- 专注核心价值：Runtime 抽象 + Artifact 治理 + 上下文隔离
- 减少前端维护负担
- 可随时切换到其他编排平台 (n8n, Langflow)

### 负面

- 依赖 Dify 的 Workflow 语义 (但 Bridge 可以抽象)
- 需要运行两个服务 (Dify + Bridge)
- Dify 的限制会传递到用户体验

### 风险缓解

- Bridge 层设计为 Runtime-agnostic
- 保留 WorkflowEngine 独立运行能力
- 预留 MCP Server 接口 (供 Claude Desktop 直接调用)

## 下一步

1. 安装并运行 Dify (Docker Compose)
2. 实现 Bridge MVP (`POST /api/bridge/execute_step`)
3. 创建 Dify Workflow Template (研究 → 实现 → 审核)
4. 测试端到端流程
5. 文档化集成方法

---

**决策者**: SOP Orchestrator Team  
**实现优先级**: High (阻塞 Phase 2)
