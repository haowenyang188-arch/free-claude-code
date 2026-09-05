# API 超时 503 问题修复总结

## ✅ 修复完成

### 问题
CC Switch API 出现超时 503 错误，后台任务无限期运行导致资源耗尽。

### 根本原因
`BridgeService._execute_in_background()` 中缺少超时控制，`await self._runner.execute()` 可能永久阻塞。

## 修改文件清单

### 1. 核心修复
- ✅ `workbench/backend/bridge/service.py`
  - 第 30-34 行：添加 `execution_timeout` 参数（默认 300s）
  - 第 90-133 行：使用 `asyncio.wait_for()` 包装执行并处理超时异常

### 2. 配置支持
- ✅ `workbench/backend/bridge/server.py`
  - 第 3 行：导入 `os` 模块
  - 第 36-38 行：从环境变量 `BRIDGE_EXECUTION_TIMEOUT` 读取配置

### 3. 测试覆盖
- ✅ `tests/workbench/test_bridge_service.py`
  - 第 3 行：导入 `asyncio`
  - 第 260-329 行：添加超时相关测试用例（4个新测试）

### 4. 文档
- ✅ `workbench/docs/FIX-API-TIMEOUT-503.md` - 完整的修复文档
- ✅ `workbench/docs/FIX-SUMMARY.md` - 本总结文档

## 快速验证

### 语法检查
```bash
✅ 已通过 Python 语法检查
cd workbench/backend && python3 -m py_compile bridge/service.py bridge/server.py
```

### 单元测试
```bash
# 运行 Bridge Service 测试
cd workbench
pytest tests/workbench/test_bridge_service.py -v

# 运行所有 Bridge 相关测试
pytest tests/workbench/test_bridge*.py -v
```

### 手动测试
```bash
# 启动服务（使用默认 300s 超时）
cd workbench/backend
python3 -m workbench.backend.bridge.server --port 8000

# 或使用自定义超时
BRIDGE_EXECUTION_TIMEOUT=120.0 python3 -m workbench.backend.bridge.server --port 8000

# 健康检查
curl http://localhost:8000/api/bridge/health
```

## 使用方法

### 环境变量配置

```bash
# 默认：5 分钟超时
python3 -m workbench.backend.bridge.server

# 自定义：2 分钟超时
export BRIDGE_EXECUTION_TIMEOUT=120.0
python3 -m workbench.backend.bridge.server

# 自定义：10 分钟超时（复杂任务）
export BRIDGE_EXECUTION_TIMEOUT=600.0
python3 -m workbench.backend.bridge.server
```

### Docker 配置

```yaml
# docker-compose.yml
services:
  bridge:
    environment:
      - BRIDGE_EXECUTION_TIMEOUT=300.0
```

## 关键代码片段

### 超时控制核心逻辑

```python
# workbench/backend/bridge/service.py

async def _execute_in_background(
    self, task_id: str, request: ExecuteStepRequest
) -> None:
    """Execute task in background and update status."""
    try:
        # 使用 asyncio.wait_for 添加超时保护
        artifact = await asyncio.wait_for(
            self._runner.execute(
                task=task, assignment=assignment, context=context
            ),
            timeout=self._execution_timeout,  # ⏱️ 超时控制
        )
        
        # 成功：更新为 COMPLETED
        self._tasks[task_id].update({
            "status": TaskStatus.COMPLETED,
            "updated_at": datetime.now(UTC),
            "artifact": artifact,
        })
        
    except asyncio.TimeoutError:  # ⚠️ 捕获超时
        self._tasks[task_id].update({
            "status": TaskStatus.FAILED,
            "updated_at": datetime.now(UTC),
            "error": f"Execution timed out after {self._execution_timeout}s",
        })
        
    except Exception as e:  # ⚠️ 捕获其他异常
        self._tasks[task_id].update({
            "status": TaskStatus.FAILED,
            "updated_at": datetime.now(UTC),
            "error": str(e),
        })
```

## 错误响应示例

### 超时场景

**请求**:
```bash
POST /api/bridge/execute_step
{
  "workflow_run_id": "dify-run-123",
  "node_id": "researcher-node",
  "role": "security_researcher",
  "capability": "research_oauth2",
  "goal": "Deep security audit",
  "runtime_kind": "claude_code"
}
```

**立即响应** (202 Accepted):
```json
{
  "task_id": "task-abc123",
  "status": "running",
  "polling_url": "/api/bridge/tasks/task-abc123"
}
```

**轮询响应** (超时后):
```bash
GET /api/bridge/tasks/task-abc123
```

```json
{
  "task_id": "task-abc123",
  "status": "failed",
  "created_at": "2026-08-31T10:00:00Z",
  "updated_at": "2026-08-31T10:05:00Z",
  "artifact": null,
  "error": "Execution timed out after 300.0s"
}
```

## 兼容性

### 向后兼容
✅ **完全兼容** - 现有代码无需修改
- 默认超时 300 秒符合其他运行时（Claude: 240s, DSH: 300s）
- `execution_timeout` 参数是可选的
- 环境变量未设置时使用默认值

### 现有调用者
所有现有的 `BridgeService(runner=runner)` 调用无需修改即可工作。

## 监控建议

### 关键指标

```python
# Prometheus metrics (建议添加)
bridge_execution_timeout_total{role, capability}  # 超时任务总数
bridge_execution_duration_seconds{role, capability}  # 执行时长分布
bridge_task_status_total{status}  # 任务状态分布
```

### 日志查询

```bash
# 查找超时任务
grep "timed out after" /var/log/bridge.log

# 统计超时频率
grep "timed out after" /var/log/bridge.log | wc -l
```

## 下一步

### 立即行动
1. ✅ 代码已修复并通过语法检查
2. ⏳ 运行完整测试套件
3. ⏳ 在测试环境验证
4. ⏳ 部署到生产环境
5. ⏳ 监控超时率

### 后续优化（可选）
- [ ] 根据任务类型动态调整超时时间
- [ ] 添加超时任务重试机制
- [ ] 集成 Prometheus 监控
- [ ] 实现任务取消功能
- [ ] 引入消息队列处理长时间任务

## 参考

- 详细文档：[FIX-API-TIMEOUT-503.md](./FIX-API-TIMEOUT-503.md)
- 核心代码：[bridge/service.py](../backend/bridge/service.py)
- 测试用例：[test_bridge_service.py](../../tests/workbench/test_bridge_service.py)

---

**修复日期**: 2026-08-31  
**修复作者**: Claude Code  
**状态**: ✅ 代码修复完成，等待测试验证
