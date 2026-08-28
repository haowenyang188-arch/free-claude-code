# Claude Code 开发规范

**基于 Qoder Rules 提炼，适用于 AI 辅助开发**

---

## 0. 核心原则（Andrej Karpathy）

**Behavioral guidelines to reduce common LLM coding mistakes.**

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 0.1 Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 0.2 Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 0.3 Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 0.4 Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

## 1. 需求理解规则

### 1.1 明确需求，不假设
- 不确定时主动询问，不要猜测
- 存在多种解释时，呈现所有选项
- 如果有更简单的方案，明确说明
- 遇到不清楚的地方，停止并询问

### 1.2 复用现有代码
- 检查现有代码库，优先使用已有接口和 API
- 不要重复造轮子
- 保持与现有接口的兼容性

### 1.3 最小化依赖
- 优先使用项目已有依赖
- 新增依赖需有明确理由
- 优先使用内置库而非外部包

---

## 2. 代码修改规则

### 2.1 只修改请求的内容
- 不进行未经授权的重构
- 保持原有逻辑，除非明确要求改进
- 专注于具体任务需求
- 保留现有代码结构

### 2.2 精准修改
- 每一行修改都应直接关联用户请求
- 不"改进"相邻代码、注释或格式
- 不重构未损坏的代码
- 匹配现有风格，即使你会有不同做法

### 2.3 清理自己的烂摊子
- 删除因你的修改而变得无用的导入/变量/函数
- 不删除预先存在的死代码，除非被要求

---

## 3. 禁止编造 API/路径/函数规则

### 3.1 验证 API 存在性
- 使用前验证 API 是否存在
- 检查文档确认正确的 API 签名
- 只使用已确认可用的 API
- 不编造虚构的库方法
- 测试 API 与项目版本的兼容性

### 3.2 只使用真实存在的库
- 导入前验证包是否存在
- 不使用虚构或编造的包
- 检查 npm/pip/包注册表
- 确认库版本兼容性

### 3.3 常见错误示例
❌ 错误：
```javascript
array.removeAt(index);  // 不存在的方法
import { magicHelper } from 'super-magic-lib';  // 不存在的库
```

✅ 正确：
```javascript
array.splice(index, 1);  // 标准方法
import { useState } from 'react';  // 真实库
```

---

## 4. 测试验证规则

### 4.1 测试完整性
- 新功能必须包含单元测试
- Bug 修复必须包含回归测试
- 公共 API 变更必须更新集成测试

### 4.2 测试覆盖率目标
- Web 应用：最低 70% 行覆盖率
- CLI 工具：最低 80% 行覆盖率
- 库/SDK：最低 85% 行覆盖率，公共 API 100%

### 4.3 测试分层
- 单元测试（70%）：快速、隔离、针对单个函数/类
- 集成测试（20%）：验证模块间交互
- E2E 测试（10%）：关键业务流程端到端验证

### 4.4 Mock 规范
- 外部依赖（API、数据库、文件系统）必须 Mock
- 不要 Mock 被测试的核心逻辑
- 使用真实数据结构，避免过度简化的 Mock

### 4.5 边界条件测试
- 测试空值、null、undefined
- 测试边界值（0、负数、最大值、最小值）
- 测试异常输入和错误处理路径

---

## 5. 报错处理规则

### 5.1 错误分类
- **业务错误**：验证失败、业务规则违反（用户可恢复）
- **系统错误**：数据库连接失败、文件 I/O 错误（需运维介入）
- **第三方错误**：外部 API 调用失败（需降级处理）

### 5.2 错误处理原则
- 始终处理错误，绝不静默失败
- 使用 try-catch 处理异步操作
- 记录错误时包含足够上下文
- 返回用户友好的错误消息
- 不在错误消息中暴露敏感信息

### 5.3 错误日志记录
- 业务错误：INFO 或 WARN 级别
- 系统错误：ERROR 级别
- 记录上下文：用户 ID、请求 ID、时间戳
- 不记录敏感数据（密码、令牌、信用卡）

### 5.4 错误恢复策略
- 外部服务失败：重试 + 指数退避
- 非关键功能失败：降级到默认值
- 设置最大重试次数和超时时间

---

## 6. 安全规则

### 6.1 输入验证与清理
- 验证所有用户输入、API 参数、文件上传
- 使用白名单验证，不要黑名单
- 清理 HTML/SQL/命令注入风险字符
- 验证数据类型、长度、格式、范围

### 6.2 认证与授权
- 使用成熟的认证库
- 密码至少 8 位，强制复杂度要求
- 授权检查在服务端，不依赖客户端
- 遵循最小权限原则

### 6.3 敏感数据保护
- 密码使用 bcrypt/Argon2 单向哈希，加盐
- 敏感数据加密存储
- 使用 HTTPS/TLS 传输敏感数据
- 不在日志、错误消息、URL 中暴露敏感数据

### 6.4 安全配置管理
- 不在代码中硬编码密钥、密码、API 令牌
- 使用环境变量或密钥管理服务
- 不提交 .env 文件到版本控制
- 生产环境关闭调试模式和详细错误信息

### 6.5 日志安全
- 日志中禁止记录：密码、令牌、密钥、信用卡号、身份证
- 记录安全事件：登录失败、权限拒绝、异常访问
- 日志文件权限受限

---

## 7. 最终交付规则

### 7.1 生成完整可运行代码
- 代码必须立即可执行，不允许占位符
- 包含所有必要的导入、依赖和配置
- 提供完整实现，不是部分代码片段
- 不允许 TODO 注释或"稍后实现"的注释

### 7.2 确保代码编译成功
- 交付前检查语法
- 解决所有编译错误
- 测试构建过程
- 验证导入和依赖正确

### 7.3 一次性修复错误
- 测试解决方案后再呈现
- 提供可工作的修复，不是迭代尝试
- 解决根本原因，不只是症状
- 确保代码编译并运行后再交付

### 7.4 注释与代码一致
- 代码变更时更新注释
- 提供准确、真实的文档
- 没有误导或过时的注释
- 注释应反映实际行为

### 7.5 功能优先于完美
- 可工作的代码 > 完美的代码
- 功能性优先于优化
- 先交付 MVP，再进行增强

---

## 8. 工作流规则

### 8.1 文档同步
- 修改代码时更新相关文档
- 更改端点/接口时更新 API 文档
- 添加新功能或更改设置时更新 README
- 更改函数行为时更新内联注释

### 8.2 破坏性变更协议
- 清晰记录所有破坏性变更
- 为用户提供迁移指南
- 提升 MAJOR 版本号
- 尽可能保持向后兼容
- 删除前标记已弃用功能

### 8.3 依赖更新策略
- 定期审查依赖更新
- 更新主要版本前彻底测试
- 在变更日志中记录依赖变更
- 添加新依赖前检查安全漏洞
- 避免使用已弃用或未维护的包

---

## 9. 关键规则总结

| 优先级 | 规则 | 说明 |
|--------|------|------|
| CRITICAL | 生成完整可运行代码 | 无 TODO、无占位符 |
| CRITICAL | 验证所有 API 存在 | 不编造虚构方法 |
| CRITICAL | 确保代码编译成功 | 交付前必须编译通过 |
| CRITICAL | 只使用真实存在的库 | 不导入虚构包 |
| HIGH | 复用现有接口/API | 不重复造轮子 |
| HIGH | 最小化依赖 | 使用已有依赖 |
| HIGH | 只修改请求的内容 | 不进行未经授权的重构 |
| HIGH | 一次性修复错误 | 解决根本原因 |
| MEDIUM | 注释与代码一致 | 更新时同步更新 |
| MEDIUM | 功能优先于完美 | 先交付 MVP |

---

## 10. 使用说明

### 10.1 如何让 Claude Code 遵循此规范

将此文件放在项目根目录 `CLAUDE.md`，Claude Code 会自动读取并遵循这些规则。

### 10.2 在对话中引用

在 Claude Code 对话中，可以明确提及：
```
请遵循 CLAUDE.md 中的规范完成此任务
```

### 10.3 项目类型配置

根据项目类型，可以调整规则优先级：

**Web 应用**：重点关注安全、测试覆盖率（70%）、API 验证
**CLI 工具**：重点关注依赖最小化、测试覆盖率（80%）、错误处理
**库/SDK**：重点关注 API 正确性、测试覆盖率（85%）、文档同步

---

**最后更新**：2026-05-05
**来源**：Qoder Rules (https://github.com/lvzhaobo/qoder-rules)

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **free-claude-code** (3475 symbols, 4376 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/free-claude-code/context` | Codebase overview, check index freshness |
| `gitnexus://repo/free-claude-code/clusters` | All functional areas |
| `gitnexus://repo/free-claude-code/processes` | All execution flows |
| `gitnexus://repo/free-claude-code/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

@CODE_INTELLIGENCE_WORKFLOW.md
