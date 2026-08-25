# End-to-End Testing Guide

## Prerequisites

1. **Claude Code CLI installed and accessible**
   ```bash
   which claude
   # Should output: /path/to/claude
   ```

2. **SOP Orchestrator Bridge running**
   ```bash
   cd /home/gnen/free-claude-code
   python -m workbench.backend.bridge.server
   ```

3. **Dify installed (Docker)**
   ```bash
   git clone https://github.com/langgenius/dify
   cd dify/docker
   docker compose up -d
   ```

---

## Test 1: Direct Bridge API Test (Without Dify)

### Step 1: Start Bridge Server

Create `workbench/backend/bridge/server.py`:

```python
"""Standalone Bridge server for testing."""

import uvicorn
from fastapi import FastAPI
from workbench.backend.bridge import create_bridge_router, BridgeService
from workbench.backend.workflow.runners import RuntimeNeutralRunner
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter

app = FastAPI(title="SOP Orchestrator Bridge")

# Initialize components
claude_code_adapter = ClaudeCodeAdapter()
runner = RuntimeNeutralRunner(adapters=[claude_code_adapter])
bridge_service = BridgeService(runner=runner)

# Mount Bridge routes
bridge_router = create_bridge_router(bridge_service=bridge_service)
app.include_router(bridge_router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

Start server:
```bash
python workbench/backend/bridge/server.py
```

### Step 2: Test Health Endpoint

```bash
curl http://localhost:8000/api/bridge/health
```

Expected response:
```json
{
  "status": "healthy",
  "bridge_version": "1.0.0"
}
```

### Step 3: Execute Simple Task

```bash
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "test-run-001",
    "node_id": "simple-test",
    "role": "assistant",
    "capability": "general",
    "goal": "Say hello and explain what you are",
    "runtime_kind": "claude_code"
  }'
```

Expected response:
```json
{
  "task_id": "task-abc123...",
  "status": "running",
  "polling_url": "/api/bridge/tasks/task-abc123..."
}
```

### Step 4: Poll Task Status

```bash
# Replace {task_id} with actual task ID from Step 3
curl http://localhost:8000/api/bridge/tasks/{task_id}
```

Poll every 5 seconds until status changes to `completed`:

```json
{
  "task_id": "task-abc123...",
  "status": "completed",
  "created_at": "2026-08-25T...",
  "updated_at": "2026-08-25T...",
  "artifact": {
    "id": "artifact-...",
    "type": "text",
    "content": "Hello! I am Claude, an AI assistant...",
    "summary": "Hello! I am Claude, an AI assistant..."
  },
  "error": null
}
```

### Step 5: Test with Workspace Scope

```bash
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "test-run-002",
    "node_id": "workspace-test",
    "role": "developer",
    "capability": "code_analysis",
    "goal": "List Python files in workbench/backend/bridge/",
    "instructions": "Use ls or find command",
    "runtime_kind": "claude_code",
    "workspace_scope": "/home/gnen/free-claude-code"
  }'
```

Poll for results - should list files in the bridge directory.

---

## Test 2: Multi-Step Workflow (Artifact Handoff)

### Step 1: Research Step

```bash
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "multi-step-001",
    "node_id": "research-step",
    "role": "researcher",
    "capability": "research",
    "goal": "Research best practices for Python async error handling",
    "instructions": "Focus on asyncio.TimeoutError and subprocess errors",
    "constraints": ["Keep response under 500 words"],
    "runtime_kind": "claude_code"
  }' | jq -r '.task_id' > /tmp/research_task_id.txt
```

### Step 2: Wait and Get Research Artifact

```bash
# Poll until completed
RESEARCH_TASK_ID=$(cat /tmp/research_task_id.txt)
while true; do
  STATUS=$(curl -s http://localhost:8000/api/bridge/tasks/$RESEARCH_TASK_ID | jq -r '.status')
  if [ "$STATUS" = "completed" ]; then
    curl -s http://localhost:8000/api/bridge/tasks/$RESEARCH_TASK_ID | jq -r '.artifact.id' > /tmp/research_artifact_id.txt
    curl -s http://localhost:8000/api/bridge/tasks/$RESEARCH_TASK_ID | jq -r '.artifact.content' > /tmp/research_content.txt
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "Research task failed"
    exit 1
  fi
  echo "Waiting for research... (status: $STATUS)"
  sleep 5
done
```

### Step 3: Implementation Step (Use Research)

```bash
RESEARCH_ARTIFACT_ID=$(cat /tmp/research_artifact_id.txt)
RESEARCH_CONTENT=$(cat /tmp/research_content.txt)

curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d "{
    \"workflow_run_id\": \"multi-step-001\",
    \"node_id\": \"implement-step\",
    \"role\": \"developer\",
    \"capability\": \"code_improvement\",
    \"goal\": \"Review the ClaudeCodeAdapter error handling based on research\",
    \"instructions\": \"Check if the implementation follows the best practices from research\",
    \"upstream_artifacts\": [
      {\"id\": \"$RESEARCH_ARTIFACT_ID\", \"content\": \"$RESEARCH_CONTENT\"}
    ],
    \"runtime_kind\": \"claude_code\",
    \"workspace_scope\": \"/home/gnen/free-claude-code\"
  }"
```

---

## Test 3: Error Handling

### Test Invalid Runtime

```bash
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "error-test-001",
    "node_id": "invalid-runtime",
    "role": "test",
    "capability": "test",
    "goal": "Test invalid runtime",
    "runtime_kind": "invalid_runtime"
  }'
```

Expected: `422 Unprocessable Entity` with validation error

### Test Missing Required Fields

```bash
curl -X POST http://localhost:8000/api/bridge/execute_step \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_run_id": "error-test-002"
  }'
```

Expected: `422 Unprocessable Entity` with missing field errors

### Test Unknown Task ID

```bash
curl http://localhost:8000/api/bridge/tasks/unknown-task-id
```

Expected: `404 Not Found` with "Task not found" message

---

## Test 4: Dify Integration (Full E2E)

### Step 1: Access Dify UI

```
http://localhost/install
```

Complete setup wizard.

### Step 2: Create Workflow

1. Click "Studio" → "Create Workflow"
2. Name: "SOP Orchestrator Test"

### Step 3: Add HTTP Tool Node (Research)

- **Node Name:** Research OAuth2
- **Method:** POST
- **URL:** `http://host.docker.internal:8000/api/bridge/execute_step`
- **Headers:**
  ```
  Content-Type: application/json
  ```
- **Body:**
  ```json
  {
    "workflow_run_id": "{{run_id}}",
    "node_id": "research-oauth2",
    "role": "security_researcher",
    "capability": "research",
    "goal": "{{start.goal}}",
    "instructions": "Focus on PKCE and authorization code flow",
    "constraints": ["Keep response under 1000 words"],
    "runtime_kind": "claude_code"
  }
  ```

### Step 4: Add Code Node (Poll)

```python
import time
import requests

task_id = http_research.body.task_id
polling_url = f"http://host.docker.internal:8000/api/bridge/tasks/{task_id}"

for i in range(60):  # Max 5 minutes
    response = requests.get(polling_url)
    data = response.json()
    
    if data["status"] == "completed":
        return {
            "artifact_id": data["artifact"]["id"],
            "content": data["artifact"]["content"],
            "summary": data["artifact"]["summary"]
        }
    elif data["status"] == "failed":
        raise Exception(f"Task failed: {data['error']}")
    
    time.sleep(5)

raise Exception("Task timeout")
```

### Step 5: Add Answer Node

Display `{{code_poll.content}}`

### Step 6: Run Workflow

- **Input (start.goal):** "Research OAuth2 security best practices"
- **Expected Output:** Claude Code's research response

---

## Verification Checklist

### Bridge Server
- [ ] Health endpoint returns 200
- [ ] Execute step accepts valid requests (202)
- [ ] Execute step rejects invalid requests (422)
- [ ] Poll endpoint returns running status
- [ ] Poll endpoint returns completed with artifact
- [ ] Poll endpoint returns failed with error message
- [ ] Unknown task returns 404

### Claude Code Integration
- [ ] Temp file is created with prompt
- [ ] `claude` CLI is invoked correctly
- [ ] stdout is captured as artifact content
- [ ] stderr errors are captured
- [ ] Workspace scope (--cwd) is passed correctly
- [ ] Process timeout works (5 minutes)
- [ ] Temp file is cleaned up after execution

### Multi-Step Flow
- [ ] First step produces artifact
- [ ] Second step receives upstream artifact
- [ ] Upstream artifact content appears in prompt
- [ ] Artifacts are correlated by ID

### Error Scenarios
- [ ] Invalid runtime_kind rejected at API level
- [ ] Missing required fields rejected at API level
- [ ] Claude CLI non-zero exit code → task failed status
- [ ] Claude CLI timeout → task failed status
- [ ] Empty Claude response → task failed status

---

## Troubleshooting

### Bridge server won't start
```bash
# Check if port 8000 is in use
lsof -i :8000

# Use different port
python workbench/backend/bridge/server.py --port 8001
```

### Claude CLI not found
```bash
# Verify installation
which claude

# Add to PATH if needed
export PATH="$PATH:/path/to/claude"
```

### Dify can't reach Bridge
```bash
# From Dify container, test connectivity
docker exec -it dify-api curl http://host.docker.internal:8000/api/bridge/health

# If fails, use host IP instead of host.docker.internal
ip addr show | grep inet
# Use http://<host-ip>:8000/api/bridge/execute_step
```

### Task stays in "running" forever
```bash
# Check Bridge server logs
# Check if Claude CLI is hanging
ps aux | grep claude

# Check task details
curl http://localhost:8000/api/bridge/tasks/{task_id}
```

### Artifact not passed to next step
- Verify `upstream_artifacts` contains correct artifact ID
- Verify artifact content is included in request body
- Check if artifact_store is initialized (currently None in ClaudeCodeAdapter)

---

## Success Criteria

✅ **Phase 5 Complete** when:
1. Direct Bridge API test passes (Test 1)
2. Multi-step workflow with artifact handoff works (Test 2)
3. Error handling works correctly (Test 3)
4. Dify workflow executes end-to-end (Test 4)
5. All verification checklist items pass

---

**Next:** Document results and create production deployment guide
