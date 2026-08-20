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
