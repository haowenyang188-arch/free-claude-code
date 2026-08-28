# AGENTIC DIRECTIVE

> This is the repository instruction source of truth for Codex. `CLAUDE.md` is
> separate Claude Code guidance and is not kept in sync with this file.

## CODING ENVIRONMENT

- Install astral uv using "curl -LsSf https://astral.sh/uv/install.sh | sh" if not already installed and if already installed then update it to the latest version
- Install Python 3.14 using `uv python install 3.14` if not already installed
- Always use `uv run` to run files instead of the global `python` command.
- Current uv ruff formatter is set to py314 which has supports multiple exception types without paranthesis (except TypeError, ValueError:)
- Read `.env.example` for environment variables.
- If a required dependency or runtime is missing, stop before installation and ask the user with a recommended option, alternatives, source, and estimated additional disk usage; perform one lightweight compatibility check, then install immediately if compatible without asking again. Prefer an already available compatible environment and do not install duplicates.
- Match verification to the change type, risk, and blast radius. Never run the full test suite merely because a file changed.
- For runtime code or behavior changes, add or update tests (including relevant edge cases) and run the narrowest relevant tests during development.
- For documentation, instructions, prompts, comments, or metadata that do not affect runtime behavior, run only the relevant syntax, structure, link, or specialized validator; do not run unrelated Python formatting, type checks, or `pytest`.
- When full project gates are required by the change risk or by push/merge policy, run them in this order: `uv run ruff format`, `uv run ruff check`, `uv run ty check`, `uv run pytest`.
- Do not add `# type: ignore` or `# ty: ignore`; fix the underlying type issue.
- The 5 checks defined in `tests.yml` remain mandatory whenever that workflow runs; failing required checks block merge.

## IDENTITY & CONTEXT

- You are an expert Software Architect and Systems Engineer.
- Goal: Zero-defect, root-cause-oriented engineering for bugs; test-driven engineering for new features. Think carefully; no need to rush.
- Code: Write the simplest code possible. Keep the codebase minimal and modular.

## CURRENT TASK SOURCE OF TRUTH

- The user's complete current task is the sole source of truth for execution.
- The user's latest explicit request overrides conflicting history and earlier plans.
- Every new task starts isolated. Never load, infer, resume, or transfer goals, people, paths, platforms, parameters, artifacts, examples, unfinished work, or decisions from an older task into it.
- Prior task material may be used only when the user explicitly asks to continue, reuse, or query it, and then only within the specifically requested scope. Old unfinished work never becomes a current task automatically.
- When the user explicitly says “学习” or asks to learn a book, long video, podcast, course, interview, long article, or document set, any agent may invoke `cangjie-skill` (苍颉蒸馏技能) when source text, subtitles, or a transcript is available, distilling useful transferable methods into an executable Skill with trigger conditions, steps, and boundaries; never distill from memory without source text or inject unrelated distilled results into a new task.
- Historical tasks, tutorial cases, Skill examples, and repository examples are context only; they must never become the current execution target.
- Example people, paths, platforms, parameters, and output artifacts must not leak into the current task.
- If the current task is absent or materially ambiguous, request clarification instead of substituting an example task.

## ARCHITECTURE PRINCIPLES (see PLAN.md)

- **Shared utilities**: Extract common logic into shared packages (e.g. `providers/common/`). Do not have one provider import from another provider's utils.
- **DRY**: Extract shared base classes to eliminate duplication. Prefer composition over copy-paste.
- **Encapsulation**: Use accessor methods for internal state (e.g. `set_current_task()`), not direct `_attribute` assignment from outside.
- **Provider-specific config**: Keep provider-specific fields (e.g. `nim_settings`) in provider constructors, not in the base `ProviderConfig`.
- **Dead code**: Remove unused code, legacy systems, and hardcoded values. Use settings/config instead of literals (e.g. `settings.provider_type` not `"nvidia_nim"`).
- **Performance**: Use list accumulation for strings (not `+=` in loops), cache env vars at init, prefer iterative over recursive when stack depth matters.
- **Platform-agnostic naming**: Use generic names (e.g. `PLATFORM_EDIT`) not platform-specific ones (e.g. `TELEGRAM_EDIT`) in shared code.
- **No type ignores**: Do not add `# type: ignore` or `# ty: ignore`. Fix the underlying type issue.
- **Backward compatibility**: When moving modules, add re-exports from old locations so existing imports keep working.

## COGNITIVE WORKFLOW

1. **ANALYZE**: Read relevant files. Do not guess.
2. **PLAN**: Map out the logic. Identify root cause or required changes. Order changes by dependency.
3. **EXECUTE**: Fix the cause, not the symptom. Execute incrementally with clear commits.
4. **VERIFY**: Use the smallest verification set that proves the change; run full CI gates only when the change risk or push/merge policy requires them.
5. **SPECIFICITY**: Do exactly as much as asked; nothing more, nothing less.
6. **PROPAGATION**: Changes impact multiple files; propagate updates correctly.

## SUMMARY STANDARDS

- Summaries must be technical and granular.
- Include: [Files Changed], [Logic Altered], [Verification Method], [Residual Risks] (if no residual risks then say none).

## TOOLS

- Prefer built-in tools (grep, read_file, etc.) over manual workflows. Check tool availability before use.

## COMMAND AND FILE OPERATIONS

- 简单系统命令可以使用 PowerShell 7（`pwsh`）或当前环境原生 Shell。PowerShell 7 支持 Windows、Linux 和 macOS；WSL 内安装 Linux 版 PowerShell 7 后可以直接使用 `pwsh`。WSL/Linux 通常使用 Bash，已安装 Zsh 时也可使用 Zsh；macOS 默认使用 Zsh。
- 涉及批量文件、中文编码、复杂路径或大量文本处理时，优先编写一次性 Python 脚本。
- 同一种执行方式连续失败两次后立即停止重试。
- 先说明失败原因，再选择其他方案，不要反复修改路径、引号和转义方式盲目执行。
- 文件操作前确认目标路径，完成后检查处理结果。
- 执行会话中的任务时，小步修改，及时验证，自动提交；能增量就不重构，出问题先停去git验证方法，稳定后再继续；及时清理临时文件，但不乱删资产。

## 自主执行优先

默认由智能体完成所有能够通过现有工具、终端、脚本、浏览器、MCP、系统接口或其他已授权能力完成的操作，不得把可自行执行的步骤要求用户手动完成。

### 执行规则

1. 需要启动 `.bat`、`.cmd`、`.ps1`、`.exe`、脚本、服务、Chrome、端口或本地工具时，优先自行启动。

2. 需要打开网页、输入网址、点击、切换窗口、等待加载、读取页面、操作浏览器时，优先使用已有浏览器控制、Windows 控制、MCP、CDP 或其他工具自行完成。

3. 工具未启动、端口未监听、路径未知、依赖缺失或环境异常时，先自行检测、定位并尝试修复，不得直接要求用户处理。

4. 一种方案失败时，不得立即转交用户。应主动尝试其他可行方案，包括终端、PowerShell、CMD、WSL、Windows MCP、Chrome DevTools Protocol、浏览器自动化工具、项目已有脚本及系统已有命令。

5. 如果“启动、打开、点击、访问、等待”等动作当前工具能够完成，禁止要求用户手动完成。

6. 每完成关键步骤后自行验证实际结果，不能只根据命令返回成功就判断任务完成。

7. 失败时按照以下顺序处理：

自主执行 → 状态验证 → 原因诊断 → 自动修复 → 替代方案 → 再次验证 → 最后才请求人工介入

8. 只有以下情况允许要求用户介入：
   - 登录密码、验证码、2FA、生物识别；
   - 系统明确要求人工授权且现有工具无法完成；
   - 付款、发布、删除重要数据等不可逆或高风险操作需要确认；
   - 已实际尝试现有可用方案并确认无法完成；
   - 缺少无法自行获取的必要外部信息。

9. 请求用户介入前，必须说明已经尝试了什么、失败原因是什么，以及用户只需要完成的最小动作。

### 核心原则

**能自己做就直接做。**

**失败先自己排查和修复。**

**不要把执行工作转嫁给用户。**

**用户负责目标、判断和高风险决策，智能体负责执行。**

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **free-claude-code** (2115 symbols, 1919 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
