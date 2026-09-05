# API 超时 503 问题修复报告

## 问题描述

**症状**：CC Switch API 调用超时，返回 503 错误

**根本原因**：
- `BridgeService._execute_in_background()` 中的 `await self._runner.execute()` 没有超时限制
- 后台任务可能无限期运行，导致客户端轮询超时
- 资源无法释放，累积导致服务不可用

## 修复方案

### 1. 添加超时控制 (service.py)

**文件**: `workbench/backend/bridge/service.py`

**更改**：
- 在 `BridgeService.__init__()` 添加 `execution_timeout` 参数（默认 300 秒）
- 在 `_execute_in_background()` 使用 `asyncio.wait_for()` 包装执行
- 添加 `asyncio.TimeoutError` 异常处理

```python
class BridgeService:
    def __init__(
        self, runner: RuntimeNeutralRunner, *, execution_timeout: float = 300.0
    ) -> None:
        self._runner = runner
        self._tasks: dict[str, dict[str, Any]] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._execution_timeout = execution_timeout  # ✅ 新增

    async def _execute_in_background(
        self, task_id: str, request: ExecuteStepRequest
    ) -> None:
        try:
            # 使用 asyncio.wait_for 添加超时保护 ✅
            artifact = await asyncio.wait_for(
                self._runner.execute(task=task, assignment=assignment, context=context),
                timeout=self._execution_timeout,
            )
            # ... 更新为 COMPLETED
        except asyncio.TimeoutError:  # ✅ 新增超时处理
            self._tasks[task_id].update({
                "status": TaskStatus.FAILED,
                "error": f"Execution timed out after {self._execution_timeout}s",
            })
        except Exception as e:
            # 处理其他异常
```

### 2. 环境变量配置支持 (server.py)

**文件**: `workbench/backend/bridge/server.py`

**更改**：
- 添加环境变量 `BRIDGE_EXECUTION_TIMEOUT` 支持
- 默认值：300 秒（5 分钟）

```python
# Read execution timeout from environment (default: 300s = 5 minutes)
execution_timeout = float(os.getenv("BRIDGE_EXECUTION_TIMEOUT", "300.0"))
bridge_service = BridgeService(runner=runner, execution_timeout=execution_timeout)
```

### 3. 测试覆盖 (test_bridge_service.py)

**新增测试用例**：
- `test_background_task_handles_timeout`: 验证超时处理逻辑
- `test_default_timeout_is_300_seconds`: 验证默认超时配置
- `test_custom_timeout_can_be_set`: 验证自定义超时配置
- `test_timeout_error_message_includes_duration`: 验证错误消息格式

## 使用指南

### 默认配置（推荐）

直接启动服务，使用默认 300 秒超时：

```bash
cd workbench/backend
python3 -m workbench.backend.bridge.server
```

### 自定义超时配置

通过环境变量设置自定义超时（单位：秒）：

```bash
# 设置 2 分钟超时
export BRIDGE_EXECUTION_TIMEOUT=120.0
python3 -m workbench.backend.bridge.server

# 或者在启动时设置
BRIDGE_EXECUTION_TIMEOUT=120.0 python3 -m workbench.backend.bridge.server
```

### Docker 部署配置

在 `docker-compose.yml` 或 Dockerfile 中设置：

```yaml
environment:
  - BRIDGE_EXECUTION_TIMEOUT=300.0
```

## 超时配置建议

| 场景 | 推荐超时 | 说明 |
|------|---------|------|
| 轻量级任务（代码审查） | 120s (2分钟) | 快速反馈 |
| 标准任务（研究、分析） | 300s (5分钟) | 默认值，适合大部分场景 |
| 复杂任务（大型重构） | 600s (10分钟) | 需要长时间处理 |
| 测试环境 | 30-60s | 快速失败，便于调试 |

## 错误处理

### 客户端行为

当任务超时时，客户端轮询 `/api/bridge/tasks/{task_id}` 会收到：

```json
{
  "task_id": "task-abc123",
  "status": "failed",
  "error": "Execution timed out after 300.0s",
  "created_at": "2026-08-31T10:00:00Z",
  "updated_at": "2026-08-31T10:05:00Z",
  "artifact": null
}
```

### 日志监控

建议监控以下指标：
- 超时任务数量（`status == "failed" && error.contains("timed out")`）
- 平均任务执行时间
- 超时任务的 `role` 和 `capability` 分布

## 参考

### 相关代码位置

- **超时配置**: `workbench/backend/bridge/service.py:30-34`
- **超时处理**: `workbench/backend/bridge/service.py:90-133`
- **环境变量**: `workbench/backend/bridge/server.py:36-38`
- **测试用例**: `tests/workbench/test_bridge_service.py:260-329`

### 其他运行时超时配置

参考其他运行时的超时设置：
- **Claude Runner**: `workbench/backend/agents/claude_runner.py:120` (默认 240s)
- **DSH Adapter**: `workbench/backend/workflow/runner_factory.py:158` (默认 300s)

## 版本信息

- **修复日期**: 2026-08-31
- **影响版本**: v1.0.0+
- **向后兼容**: ✅ 是（默认行为未改变，仅添加保护）

## 验证清单

- [x] 添加超时控制逻辑
- [x] 添加环境变量配置支持
- [x] 添加 `asyncio.TimeoutError` 异常处理
- [x] 编写单元测试覆盖超时场景
- [x] 提供清晰的错误消息
- [x] 文档说明使用方法
- [ ] 运行完整测试套件验证
- [ ] 生产环境验证

## 后续优化建议

1. **动态超时**: 根据 `role` 和 `capability` 动态调整超时时间
2. **重试机制**: 对于网络临时故障，添加指数退避重试
3. **监控告警**: 集成 Prometheus/Grafana 监控超时率
4. **熔断器**: 当超时率过高时，自动熔断保护服务
5. **任务队列**: 引入消息队列（如 Celery）解耦长时间任务
