# Token-Efficient Code Workflow

- Explore with native repository tools first; narrow searches to the required files and symbols, and use a native full-file read only when the file and small edit location are already known.
- For Claude Code, use native short Bash/PowerShell commands. Explicitly use `rtk` only for expected high-output commands such as diffs, logs, searches, tests, lint, builds, type checks, and large logs. Built-in `Read`, `Grep`, and `Glob` are suitable for source exploration.
- For Codex, explicitly prefix only expected high-output commands with `rtk` (`rtk git`, `rtk rg`, `rtk grep`, `rtk pytest`, `rtk test`, `rtk lint`, `rtk build`, and package-manager wrappers). Use the native command for short shell work or when RTK has no wrapper.
- Summarize large logs, diffs, and test output; do not paste them wholesale. Do not repeat facts already established in the current workset.
- If the repository shape is unknown, use `repomix --compress` with narrow `--include`/`--ignore`, inspect the reported token count, shrink the scope when over budget, then use `rg` and local reads. Repomix is an on-demand snapshot, not a default MCP or hook.
- Use GitNexus only for cross-file callers/callees, execution chains, impact analysis, or large refactors. Do not run it for a known file or local symbol edit.
- Use agentmemory MCP only for explicit cross-session recall or durable saves; keep automatic capture, compression, context injection, and lifecycle hooks disabled.
- Create a short checkpoint only at stage changes, context growth, or before compaction; do not add a checkpoint hook or summarize every tool call.
