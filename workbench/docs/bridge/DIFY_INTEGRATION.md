# Dify Bridge Integration Guide

## Overview

The Dify Bridge allows **Dify workflows** to execute steps through **SOP Orchestrator's runtime-neutral execution engine**, connecting visual workflow design with Claude Code/Codex CLI runtimes.

```
Dify Workflow (Visual UI)
    ↓ HTTP REST API
SOP Orchestrator Bridge
    ↓ RuntimeNeutralRunner
Claude Code / Codex CLI Runtime
```

## Architecture

### Components

```
workbench/backend/
├── bridge/
│   ├── api_models.py       # Pydantic request/response models
│   ├── service.py          # BridgeService (async task orchestration)
│   ├── routes.py           # FastAPI endpoints
│   └── __init__.py         # Package exports
├── workflow/
│   ├── runners.py          # RuntimeNeutralRunner (Phase 1)
│   └── adapters/
│       └── claude_code.py  # ClaudeCodeAdapter (Phase 1)
└── domain/
    └── models.py           # Domain models (Task, Artifact, ContextPackage)
```

### Request Flow

1. **Dify Custom Node** sends HTTP POST to `/api/bridge/execute_step`
2. **BridgeService** accepts request, spawns background task, returns 202 Accepted
3. **Background task** converts Dify request → SOP domain models (Task, Assignment, Context)
4. **RuntimeNeutralRunner** routes to appropriate adapter (ClaudeCodeAdapter)
5. **Adapter** executes task in runtime and produces Artifact
6. **Dify** polls `/api/bridge/tasks/{task_id}` until status changes to COMPLETED/FAILED

## API Reference

### POST /api/bridge/execute_step

Execute a step via Dify Bridge. Returns immediately with task ID.

**Request:**
```json
{
  "workflow_run_id": "dify-run-123",
  "node_id": "researcher-node",
  "role": "security_researcher",
  "capability": "research_oauth2",
  "goal": "Research OAuth2 best practices",
  "instructions": "Focus on PKCE and token storage",
  "constraints": ["max 1000 tokens", "use only public sources"],
  "upstream_artifacts": [
    {"id": "artifact-prev", "content": "Previous findings..."}
  ],
  "runtime_kind": "claude_code",
  "workspace_scope": "/project/auth"
}
```

**Response (202 Accepted):**
```json
{
  "task_id": "task-abc123def456",
  "status": "running",
  "polling_url": "/api/bridge/tasks/task-abc123def456"
}
```

**Fields:**
- `workflow_run_id` (required): Dify workflow run ID for correlation
- `node_id` (required): Dify node ID that triggered execution
- `role` (required): Agent role (e.g., `security_researcher`)
- `capability` (required): Capability to execute (e.g., `research_oauth2`)
- `goal` (required): Task goal/objective
- `instructions` (optional): Detailed instructions
- `constraints` (optional): Execution constraints (list of strings)
- `upstream_artifacts` (optional): Artifacts from previous Dify nodes
- `runtime_kind` (required): Runtime to use (`claude_code` | `codex`)
- `workspace_scope` (optional): Workspace directory path

### GET /api/bridge/tasks/{task_id}

Poll task status by task_id.

**Response (200 OK) - Running:**
```json
{
  "task_id": "task-abc123def456",
  "status": "running",
  "created_at": "2026-08-25T10:30:00Z",
  "updated_at": "2026-08-25T10:30:05Z",
  "artifact": null,
  "error": null
}
```

**Response (200 OK) - Completed:**
```json
{
  "task_id": "task-abc123def456",
  "status": "completed",
  "created_at": "2026-08-25T10:30:00Z",
  "updated_at": "2026-08-25T10:31:00Z",
  "artifact": {
    "id": "artifact-789abc",
    "type": "text",
    "content": "OAuth2 recommendations:\n1. Use PKCE\n2. Store tokens securely\n...",
    "summary": "OAuth2 research complete"
  },
  "error": null
}
```

**Response (200 OK) - Failed:**
```json
{
  "task_id": "task-abc123def456",
  "status": "failed",
  "created_at": "2026-08-25T10:30:00Z",
  "updated_at": "2026-08-25T10:30:15Z",
  "artifact": null,
  "error": "Runtime execution timeout after 30s"
}
```

**Response (404 Not Found):**
```json
{
  "detail": "Task not found: unknown-task-id"
}
```

### GET /api/bridge/health

Health check endpoint.

**Response (200 OK):**
```json
{
  "status": "healthy",
  "bridge_version": "1.0.0"
}
```

## Dify Integration

### Custom Node Configuration

In Dify, create a **Custom Node** with HTTP Tool:

**URL:** `http://localhost:8000/api/bridge/execute_step`  
**Method:** POST  
**Headers:**
```
Content-Type: application/json
```

**Body (use Dify variables):**
```json
{
  "workflow_run_id": "{{run_id}}",
  "node_id": "researcher-node",
  "role": "security_researcher",
  "capability": "research_oauth2",
  "goal": "{{user_input.goal}}",
  "instructions": "{{user_input.instructions}}",
  "runtime_kind": "claude_code",
  "upstream_artifacts": [
    {"id": "{{prev_node.artifact_id}}", "content": "{{prev_node.output}}"}
  ]
}
```

### Polling Strategy

**Option 1: Polling Loop in Dify**

Add a **Code Node** after the execute_step call:

```python
import time
import requests

task_id = execute_step_response["task_id"]
polling_url = f"http://localhost:8000/api/bridge/tasks/{task_id}"

max_retries = 60  # 5 minutes (60 * 5s)
for i in range(max_retries):
    response = requests.get(polling_url)
    data = response.json()
    
    if data["status"] == "completed":
        return {
            "artifact": data["artifact"]["content"],
            "summary": data["artifact"]["summary"]
        }
    elif data["status"] == "failed":
        raise Exception(f"Task failed: {data['error']}")
    
    time.sleep(5)  # Poll every 5 seconds

raise Exception("Task timeout after 5 minutes")
```

**Option 2: Webhook (Future)**

Register a webhook URL in Dify to receive completion notifications:

```json
POST {dify_webhook_url}
{
  "task_id": "task-abc123def456",
  "status": "completed",
  "artifact": {...}
}
```

## Example Workflows

### Research → Implement → Review

**Step 1: Research Node (Custom HTTP Node)**
```json
{
  "workflow_run_id": "{{run_id}}",
  "node_id": "research-oauth2",
  "role": "security_researcher",
  "capability": "research_best_practices",
  "goal": "Research OAuth2 PKCE implementation",
  "runtime_kind": "claude_code"
}
```

**Step 2: Poll & Extract**
```python
# Poll until completed, extract artifact.content
research_findings = poll_task(research_response["task_id"])
```

**Step 3: Implement Node (Custom HTTP Node)**
```json
{
  "workflow_run_id": "{{run_id}}",
  "node_id": "implement-oauth2",
  "role": "backend_developer",
  "capability": "implement_authentication",
  "goal": "Implement OAuth2 PKCE flow",
  "instructions": "Follow the research findings",
  "runtime_kind": "codex",
  "upstream_artifacts": [
    {"id": "research-artifact-id", "content": "{{research_findings}}"}
  ],
  "workspace_scope": "/project/backend/auth"
}
```

**Step 4: Human Review Gate**
Use Dify's **Human Input** node to approve/reject the implementation.

**Step 5: Deploy Node (if approved)**
```json
{
  "workflow_run_id": "{{run_id}}",
  "node_id": "deploy-oauth2",
  "role": "devops_engineer",
  "capability": "deploy_to_staging",
  "goal": "Deploy OAuth2 changes to staging",
  "runtime_kind": "claude_code",
  "constraints": ["staging environment only", "run tests first"]
}
```

## Running the Bridge

### Start the Bridge Server

```bash
# From project root
python -m uvicorn workbench.backend.bridge.routes:app --host 0.0.0.0 --port 8000
```

Or integrate into existing FastAPI app:

```python
from fastapi import FastAPI
from workbench.backend.bridge import create_bridge_router, BridgeService
from workbench.backend.workflow.runners import RuntimeNeutralRunner
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter

app = FastAPI()

# Initialize components
claude_code_adapter = ClaudeCodeAdapter()
runner = RuntimeNeutralRunner(adapters=[claude_code_adapter])
bridge_service = BridgeService(runner=runner)
bridge_router = create_bridge_router(bridge_service=bridge_service)

# Mount Bridge routes
app.include_router(bridge_router)
```

### Test the Bridge

```bash
# Health check
curl http://localhost:8000/api/bridge/health

# Execute step
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "test-run-1",
    "node_id": "test-node-1",
    "role": "researcher",
    "capability": "research",
    "goal": "Test task",
    "runtime_kind": "claude_code"
  }'

# Poll status
curl http://localhost:8000/api/bridge/tasks/{task_id}
```

## Status Codes

| Status | Meaning |
|--------|---------|
| `running` | Task is executing in background |
| `completed` | Task finished successfully, artifact available |
| `failed` | Task failed, error message available |

## Error Handling

### Validation Errors (422)

```json
{
  "detail": [
    {
      "loc": ["body", "runtime_kind"],
      "msg": "Invalid runtime_kind: invalid_runtime. Must be one of {'claude_code', 'codex'}",
      "type": "value_error"
    }
  ]
}
```

### Task Not Found (404)

```json
{
  "detail": "Task not found: unknown-task-id"
}
```

### Task Failed (200 with failed status)

```json
{
  "task_id": "task-123",
  "status": "failed",
  "error": "Runtime execution timeout after 30s"
}
```

## Troubleshooting

### Task stays in "running" forever

- Check Bridge server logs for background task exceptions
- Verify RuntimeNeutralRunner has registered adapters
- Ensure runtime (Claude Code CLI) is installed and accessible

### 404 Task Not Found

- Task IDs are only valid in the current Bridge server session
- Restarting the Bridge server clears in-memory task storage
- Future: Add persistent task storage (SQLite/Redis)

### Validation errors on execute_step

- Ensure `runtime_kind` is exactly `claude_code` or `codex` (case-sensitive)
- All required fields must be present: `workflow_run_id`, `node_id`, `role`, `capability`, `goal`, `runtime_kind`
- `upstream_artifacts` must be a list (use `[]` for empty)

## Future Enhancements

- [ ] Webhook support for async notifications
- [ ] Persistent task storage (survive server restarts)
- [ ] Task cancellation endpoint
- [ ] Streaming execution logs via WebSocket
- [ ] Artifact download endpoint (`GET /api/bridge/artifacts/{artifact_id}`)
- [ ] Multi-runtime parallelization
- [ ] Task retry with exponential backoff

## Related Documentation

- [ADR-002: Dify Integration Strategy](decisions/ADR-002-dify-integration-strategy.md)
- [RuntimeNeutralRunner](../workflow/runners.py)
- [ClaudeCodeAdapter](../workflow/adapters/claude_code.py)
- [Domain Models](../domain/models.py)
