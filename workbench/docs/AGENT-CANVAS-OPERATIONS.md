# Agent Canvas 操作说明

## 入口

- Agent Canvas：`http://127.0.0.1:8010/`
- SOP Workbench：`http://127.0.0.1:3000/`
- Workbench 会话历史：`http://127.0.0.1:3000/agent`（自动列出 18000 会话，显示事件历史并可继续发送）
- 测试 Workspace：由 Agent Canvas 的「打开工作区」选择 `Agent Canvas Test Workspace`

## 固定协作

Agent Canvas 的一个会话只运行一个 Agent Profile。完整协作链由 SOP Workbench 统一调度：

1. **Claude Code**：理解需求、分析代码、产出 PLAN。
2. **DSH Desktop**：按 PLAN 修改文件，运行命令和窄范围测试。
3. **Codex**：独立检查 diff、测试、边界和回归，输出 PASS、REWORK 或 PLAN_INVALID。

SOP Engine 拥有状态和路由权限。Artifact、Handoff、事件和测试报告是阶段交接事实；聊天记录不能替代审核证据。

## 新建会话前核验

1. 打开「协作与工具指南」，确认当前 Profile 和类型。
2. 打开「切换代理配置文件」，确认 `sop-workbench` 旁的勾选状态；只有在已恢复对应外部账号后才选择其他 ACP profile。
3. 选择测试 Workspace 后再发送消息。
4. 会话详情中的 `ACP`、Agent 名称和事件应与所选 Profile 一致。

只看到 `Default (recommended)` 不足以证明使用了 OpenHands 或 Claude；它是模型选项，不是 Agent Profile 名称。

## 工具操作顺序

- **文件**：先确认 Workspace 和目标路径，再读取或编辑。
- **终端**：先运行 `pwd`、`git status`，再运行与改动直接相关的测试。
- **Git**：先检查 diff，再决定是否提交；保留 diff 和 test report 供 Codex 审核。
- **错误**：保留 HTTP 状态、事件序号和原始错误上下文，不把失败改写成成功。

## Workbench 访问 token

Workbench 的访问 token 是 `workbench/launcher.sh` 为本机控制台生成的登录凭证，不是 Agent Canvas 的会话密钥。

在仓库根目录运行：

```bash
cd /home/gnen/free-claude-code
./workbench/launcher.sh start
```

启动命令会在当前终端显示 token；服务已经运行时再次执行 `start` 也会显示当前 token。只读查看状态：

```bash
./workbench/launcher.sh status
```

不要把 Agent Canvas（8010）的会话密钥粘贴到 Workbench 登录页，也不要把 Workbench token 配置到 Agent Canvas。

Workbench 的「Agent 会话」页面通过 8000 的 Canvas 集成代理读取完整 UUID 会话和
`events/search` 历史；可从列表打开原生 Canvas，也可直接在 Workbench 历史面板继续对话。

## Profile 名称

Profile 名称是技术标识，用于文件名和 API 路径：必须以英文字母或数字开头，只能包含英文字母、数字、点、下划线或连字符，最多 64 个字符。设置页的「重命名」会保留 Profile ID，只改变这个技术标识。

## 本机增强位置

本机 Agent Canvas 的指南、Profile 重命名和状态端点属于用户级安装目录的本地增强：

- `~/.hermes/node/lib/node_modules/@openhands/agent-canvas/build/agent-profile-rename.js`
- `~/.hermes/node/lib/node_modules/@openhands/agent-canvas/scripts/static-server.mjs`
- `~/.hermes/node/lib/node_modules/@openhands/agent-canvas/scripts/ingress.mjs`
- `~/.hermes/node/lib/node_modules/@openhands/agent-canvas/scripts/dev-with-automation.mjs`

升级或重新安装 Agent Canvas 后先检查这四个文件，再访问 8010 验证指南和 Profile 状态；不要把会话密钥或 Workbench token 写回仓库。

## 重启与网络

启动器会读取 Agent Canvas 的默认 Agent Server 版本；如果该版本已经存在于本机 `uv` 缓存，会自动使用离线解析，避免 PyPI 短暂不可用时出现“页面能打开、API 502”。需要主动刷新依赖时，可在启动前设置 `AGENT_CANVAS_ONLINE_RESOLVE=1`，完成升级后再恢复默认启动方式。
