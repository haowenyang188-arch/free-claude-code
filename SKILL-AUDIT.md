# Skill / Plugin Audit

审计日期：2026-08-13

审计范围：当前项目 `/home/gnen/free-claude-code` 的 Codex 运行时，以及用户级、共享 Agent、系统级安装目录。运行时基线为本会话提供的 Skill catalog；它是“当前项目可自动发现”的权威输入，不把缓存、备份或普通仓库文件算作已安装 Skill。

## 口径与结论

- 当前会话可发现 **85 个 Skill**：系统层 5、用户 Codex 层 45、共享 Agent 层 8、项目层 27。
- Codex Plugin Manager 的已安装插件为 **0**：`codex plugin list --json` 返回 `{"installed":[],"available":[]}`。
- `.codex/.tmp/plugins` 是 marketplace 缓存，不是安装状态；其中出现的 Superpowers 或其他 manifest 不计入已安装插件。
- 6 个候选项都已经作为当前项目 Skill 存在；它们不是用户级全局副本，但当前项目中可自动发现。因此没有任何 `INSTALL` 项，避免形成同名、不同路径的重复路由。
- “调用次数”只采用历史会话中实际读取 `SKILL.md` 的显式读取下限；没有记录时写“未检出”，不推断为从未使用。自动触发没有完整遥测。
- 来源中的 upstream、author、version 只在本地 frontmatter、链接目标、Git metadata 或项目记录可验证时填写；未联网核验维护活跃度，因此不以“未核验”作为删除理由。

## KEEP

以下项目功能仍有独立价值，或是基础路由、系统能力、明确依赖项、MCP 配套项或用户自定义项。重复关系表示“有交集但没有被完整覆盖”。

### System Skills (5)

| 名称 | 来源 / 自定义 | 用途 / 自动触发范围 | 重复与被依赖 | 显式调用下限 | 结论 |
|---|---|---|---|---:|---|
| `imagegen` | Codex 内置系统；否 | 需要生成或编辑位图、纹理、透明背景、插图或素材时 | 无完整替代；系统能力 | 未检出 | KEEP |
| `openai-docs` | Codex 内置系统；否 | 询问 Codex、OpenAI 产品/API、模型、设置、定时任务、故障排查或自我知识时 | `research-translate-apply` 可做外部研究，但不能替代 OpenAI 官方路由 | 未检出 | KEEP |
| `plugin-creator` | Codex 内置系统；否 | 创建/更新 Codex Plugin、manifest、marketplace 目录时 | `skill-creator` 只覆盖 Skill，不覆盖 Plugin | 未检出 | KEEP |
| `skill-creator` | Codex 内置系统；否 | 创建或更新可复用 Skill 时 | `plugin-creator` 不覆盖 Skill；被 Skill 创建工作流依赖 | 未检出 | KEEP |
| `skill-installer` | Codex 内置系统；否 | 用户要求列出、安装 curated 或 GitHub Skill 时 | 基础安装管理器；禁止自动删除 | 未检出 | KEEP |

### User Codex Skills (45)

| 名称 | 来源 / 自定义 | 用途 / 自动触发范围 | 重复与被依赖 | 显式调用下限 | 结论 |
|---|---|---|---|---:|---|
| `agent-five-elements-mvp` | 本地蒸馏，带 source_book；是 | 从模糊 Agent 想法定义角色、目标、工具、规则、输出的单 Agent MVP 时 | 是其他 Agent 专项 Skill 的基础；无完整替代 | 未检出 | KEEP |
| `agent-memory-progressive-layering` | 本地蒸馏；是 | 判断对话历史、跨会话记忆、外部知识库或 RAG 层级时 | 与 `second-brain`、`context-workset-retrieval` 交集不同：它做架构判断 | 未检出 | KEEP |
| `agent-mvp-realistic-evaluation` | 本地蒸馏；是 | 已有 Agent MVP，需要真实口语、错别字、模糊意图、缺省上下文评测时 | 与测试 Skill 交集有限；被 Agent MVP 体系引用 | 未检出 | KEEP |
| `agent-tool-single-responsibility` | 本地蒸馏；是 | Agent 工具 schema 过宽、参数过多、职责或副作用边界不清时 | 是 Agent MVP 设计链的一部分 | 未检出 | KEEP |
| `agent-workflow-multi-agent-gate` | 本地蒸馏；是 | 已有单 Agent 基线、需要判断是否升级多 Agent 或选择编排模式时 | 与 `orchestrate-multi-agent-tasks` 不同：先做架构门控，不负责实际启动 | 未检出 | KEEP |
| `animate` | Emil Kowalski 体系本地 Skill；否 | 实现 UI 动画、过渡或交互动效时 | 与 `review-animations` 配套；正文提到缺失的邻接项，但自身可用 | 未检出 | KEEP |
| `build-graph` | code-review-graph 配套；未证实自定义 | 初始化或更新代码审查知识图谱时 | 依赖 `code-review-graph` MCP；被图谱审查链依赖 | 未检出 | KEEP |
| `cangjie-skill` | 第三方/本地 Skill；否 | 用户明确要求把书、长视频、播客、课程、访谈或长文蒸馏为可执行 Skill 时 | 无完整替代；受 AGENTS.md 学习规则约束 | 未检出 | KEEP |
| `cli-anything` | `HKUDS/CLI-Anything` Codex 适配；否 | 构建、测试、验证或列出 GUI 应用的 CLI-Anything harness 时 | 无完整替代 | 未检出 | KEEP |
| `codex-skill-budget` | 本地 Codex 专项；是 | 出现技能简介预算截断，或安装/更新 Skill 后需要审计和原子发布时 | 唯一的预算/发布安全工具；当前审计实际使用 | 未检出 | KEEP |
| `complementary-perspective-probing` | 本地蒸馏；是 | 已决定并行探路，需要分配互补、不重复观察视角时 | 与依赖路由、实际编排 Skill 配套但职责不同 | 未检出 | KEEP |
| `context-contract-and-synthesis-acceptance` | 本地蒸馏；是 | 多 Agent 需要统一术语、交接字段、来源和未知项，或核对冲突结果时 | 编排链的交接/验收层；无完整替代 | 未检出 | KEEP |
| `context-workset-retrieval` | 本地蒸馏；是 | 跨轮接续、长文检索、仓库资料选择或 Context Window 受限时 | 与 `context-engineering` 互补：它聚焦资料选择/检索 | 未检出 | KEEP |
| `dcg` | `Dicklesworthstone/destructive_command_guard`；否 | 需要 Destructive Command Guard 的规则审计、安装或维护时 | 安全钩子配套；不能由一般安全 Skill 完整替代 | 未检出 | KEEP |
| `debug-issue` | code-review-graph 配套；未证实自定义 | 用图谱导航定位代码问题、调用链和根因时 | 依赖 `code-review-graph` MCP；与项目级调试 Skill 互补 | 未检出 | KEEP |
| `dependency-aware-agent-routing` | 本地蒸馏；是 | 多 Agent 方案已确定，需要设计 DAG、批次、交接顺序或等待条件时 | 与实际编排 Skill 配套；不负责判断是否多 Agent | 未检出 | KEEP |
| `explore-codebase` | code-review-graph 配套；未证实自定义 | 用知识图谱理解代码库结构、关系和入口时 | 依赖 `code-review-graph` MCP；与普通文件检索互补 | 未检出 | KEEP |
| `grill-with-docs` | 有效符号链接到共享 Agent Skill；未证实自定义 | 需要用领域模型质询计划并同步 CONTEXT/ADR 时 | 与对抗审查有交集，但绑定文档/领域模型；同一物理 Skill 不重复计数 | 未检出 | KEEP |
| `hallmark` | `Nutlope/Hallmark` 体系；否 | 新建、审计、重设计或提取网页 UI，尤其需要去 AI 味时 | 与前端工程 Skill 交集，但更偏设计审计和视觉质量 | 未检出 | KEEP |
| `instructor` | 有效符号链接到共享 Agent Skill；否 | 使用 Instructor + Pydantic 提取校验后的结构化 LLM 输出时 | 无完整替代；同一物理 Skill 不重复计数 | 未检出 | KEEP |
| `khazix-writer` | 本地写真提示词专项；是 | 生图前复审/改写写真提示词，去除 AI 广告片感时 | 与小红书工作流配套但更窄；无完整替代 | 未检出 | KEEP |
| `libtv-cli` | LibTV 官方 CLI 文档型 Skill；否 | 任何 LibTV 画布、项目、节点、模型或素材操作时 | 专用 CLI 路由；无完整替代 | 未检出 | KEEP |
| `manim-video` | `video-use` 内嵌子 Skill；否 | 数学/技术动画、算法或方程可视化时 | 被 `video-use` 内嵌/调用；不是独立重复安装 | 未检出 | KEEP |
| `mcp-builder` | Microsoft MCP 指南体系；否 | 创建高质量 Python/TS/C# MCP server 和工具 schema 时 | 与 Agent 工具 schema Skill 互补；无完整替代 | 未检出 | KEEP |
| `model-host-observation-loop` | 本地蒸馏；是 | 实时数据、工具/API、副作用或条件化多步任务需要确认真实执行回执时 | 与上下文/指令作用域 Skill 组合但职责不同 | 未检出 | KEEP |
| `obsidian:defuddle` | 链接到 `kepano/obsidian-skills`；否 | 提供 URL，需要用 Defuddle 去除网页噪声并提取 Markdown 时 | Obsidian 套件专用；同一 upstream，不与普通网页 Skill 合并 | 未检出 | KEEP |
| `obsidian:json-canvas` | 链接到 `kepano/obsidian-skills`；否 | 创建/编辑 `.canvas`、节点、边、组和连接时 | Obsidian 套件专用 | 未检出 | KEEP |
| `obsidian:obsidian-bases` | 链接到 `kepano/obsidian-skills`；否 | 创建/编辑 Obsidian Bases、视图、过滤器、公式和汇总时 | Obsidian 套件专用 | 未检出 | KEEP |
| `obsidian:obsidian-cli` | 链接到 `kepano/obsidian-skills`；否 | 通过 Obsidian CLI 管理 Vault、笔记、任务、属性或插件时 | Obsidian 套件专用 | 未检出 | KEEP |
| `obsidian:obsidian-markdown` | 链接到 `kepano/obsidian-skills`；否 | 编写/编辑 Obsidian Flavored Markdown、wikilink、embed、callout 时 | Obsidian 套件专用 | 未检出 | KEEP |
| `orchestrate-multi-agent-tasks` | 本地编排规则；是 | 任务含至少两个独立、可验证子问题且并行有明显收益，需要实际创建子智能体时 | 被本机 AGENTS.md 明确要求；与路由/交接 Skill 形成链 | 382 | KEEP |
| `planning-with-files` | OthmanAdi v3.9.0 体系；否 | 任务真实跨 Session、需持久化计划或从 Context loss 恢复时 | 与普通任务拆解 Skill 不同：它要求跨 Session 价值 | 未检出 | KEEP |
| `refactor-safely` | code-review-graph 配套；未证实自定义 | 需要依赖分析来规划和执行安全重构时 | 依赖 `code-review-graph` MCP；与代码简化互补 | 未检出 | KEEP |
| `research-translate-apply` | 本地改编；是 | 联网研究、读官方文档、核验最新资料并转译为代码/配置/方案时 | 与 `agent-reach`、`openai-docs` 有交集但覆盖跨来源转译 | 未检出 | KEEP |
| `review-animations` | Emil Kowalski 体系；否 | 审查 UI 动效和 motion 代码质量时 | 与 `animate` 配套；无完整替代 | 未检出 | KEEP |
| `review-changes` | code-review-graph 配套；未证实自定义 | 用变更检测和影响分析做结构化代码审查时 | 与通用审查有交集，但图谱上下文不同；依赖 MCP | 未检出 | KEEP |
| `review-delta` | code-review-graph 配套；未证实自定义 | 只审查最近提交后的 delta，需要低上下文成本和 blast-radius 分析时 | 与其他图谱审查项互补；依赖 MCP | 未检出 | KEEP |
| `review-pr` | code-review-graph 配套；未证实自定义 | 审查 PR/分支 diff 并输出图谱影响分析时 | 与 GitNexus PR 审查交集，但依赖不同图谱后端 | 未检出 | KEEP |
| `scoped-instructions-reusable-behavior` | 本地蒸馏；是 | 判断规则应放 User Prompt、Agent instructions 还是 Skill，或设计可复用输出协议时 | 与上下文/工具闭环 Skill 组合但职责不同 | 未检出 | KEEP |
| `second-brain` | 本机正式第二大脑协议；是 | 每个新任务读取 Vault 根 `AGENTS.md` 硬约束；只有用户明确授权才读写历史 | 用户明确禁止自动删除/绕过；基础路由和合规依赖项 | 899 | KEEP |
| `video-shotcraft` | `Vincentwei1021/video-shotcraft`，含本地兼容修改；定制 | 用户要求用 shot recipe/Remotion 制作电影感产品视频时 | 被小红书工作流等引用；与 `video-use` 互补 | 未检出 | KEEP |
| `video-use` | `browser-use/video-use`，含本地兼容文件；定制 | 用户要求转写、剪切、调色、字幕或视频 overlay 时 | 内嵌 `manim-video`；无完整替代 | 未检出 | KEEP |
| `wigolo` | `KnockOutEZ/wigolo` 0.1.43-beta.2；否 | 所有网页搜索、抓取、爬取、缓存、提取和研究的 umbrella 路由时 | 正文引用 10 个禁用子路由；不能拆删；与 `agent-reach` 平台范围不同 | 未检出 | KEEP |
| `xianyu-ops` | 本地闲鱼专项；是 | 闲鱼商品搜索、详情、会话读取和回复时 | 专用平台工作流；无完整替代 | 未检出 | KEEP |
| `xiaohongshu-photo-workflow` | 本地小红书写真工作流；是 | 小红书写真采集、复刻、生图、RunningHub 和文案时 | 调用 `video-shotcraft` 等；专用工作流 | 810 | KEEP |

### Shared Agent Skills (8)

| 名称 | 来源 / 自定义 | 用途 / 自动触发范围 | 重复与被依赖 | 显式调用下限 | 结论 |
|---|---|---|---|---:|---|
| `agent-reach` | `Panniantong/Agent-Reach`；否 | 用户要求联网搜索、URL 查找或指定平台查询时 | 与 `wigolo`、`research-translate-apply` 交集；平台适配不同 | 未检出 | KEEP |
| `gitnexus-guide` | GitNexus 配套；否 | 用户询问 GitNexus 工具、图谱 schema 或工作流时 | GitNexus 套件基础说明；依赖 GitNexus MCP | 未检出 | KEEP |
| `gitnexus-impact-analysis` | GitNexus 配套；否 | 修改前询问影响面、依赖或“是否安全”时 | 与图谱审查套件交集；依赖 GitNexus MCP | 未检出 | KEEP |
| `gitnexus-pr-review` | GitNexus 配套；否 | 审查 PR、合并风险或缺失测试时 | 与 `review-pr` 有交集，但后端和工作流不同 | 未检出 | KEEP |
| `jianying-capcut-drafts` | 本地专项，未声明 upstream；未证实 | 创建、检查或自动化剪映/CapCut 草稿项目时 | 专用媒体工作流；无完整替代 | 未检出 | KEEP |
| `officecli` | officecli 产品 Skill；否 | 创建、分析、校对或修改 docx/xlsx/pptx 时 | 专用 CLI；无完整替代 | 未检出 | KEEP |
| `opengod` | `381487190/opengod`；否 | 用户明确点名 OpenGod，要求复杂调研、开发、部署或复盘时 | 明确点名才触发；不是通用重复路由 | 未检出 | KEEP |
| `personal-agent` | 本地个人路由；是 | 任务涉及历史决定、长期项目、个人资料、偏好或工作记录时 | 与 `second-brain` 交集，但承担个人记忆路由；禁止自动删除 | 未检出 | KEEP |

### Project Skills (27)

项目层 Skill 由当前仓库携带，Git remote 为 `Alishahryar1/free-claude-code`。其中 22 个由 `skills-lock.json` 记录来源为 `addyosmani/agent-skills`，并由当前仓库 Git 跟踪；另有下方单列的 5 个未跟踪目录。项目层不等价于用户级全局安装，但在本项目中会自动发现。

| 名称 | 来源 / 自定义 | 用途 / 自动触发范围 | 重复与被依赖 | 显式调用下限 | 结论 |
|---|---|---|---|---:|---|
| `api-and-interface-design` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | REST/GraphQL、模块边界、公共类型和接口契约设计时 | 与规格 Skill 交集但专注 API；无完整替代 | 未检出 | KEEP |
| `archify` | 项目未跟踪；metadata author `tt-a1i` | 架构、流程、时序、数据流、生命周期图 HTML 可视化时 | 与文档 Skill 交集；来源/升级/删除风险未完全确认 | 未检出 | REVIEW |
| `browser-testing-with-devtools` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 浏览器 UI 构建/调试，需要 DOM、console、network、性能或视觉验证时 | 被 TDD 引用；声明 Chrome DevTools MCP，但当前配置未发现该 MCP | 101 | REVIEW |
| `ci-cd-and-automation` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 设置/修改 CI/CD、质量门、测试 runner 或部署自动化时 | 引用通用调试 Skill；与发布 Skill 互补 | 未检出 | KEEP |
| `code-review-and-quality` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 合并前对自己、其他 Agent 或人工改动做多轴代码审查时 | 与图谱/GitNexus 审查交集，但提供通用审查；被其他项目 Skill 引用 | 202 | KEEP |
| `code-simplification` | `addyosmani/agent-skills`（锁文件；正文注明改编自 Anthropic code-simplifier）；否 | 不改变行为地简化代码、降低复杂度时 | 与图谱重构 Skill 交集但不要求图谱 | 未检出 | KEEP |
| `commit-archaeologist` | 项目未跟踪；author Matt Van Horn | 需要从 Git 历史重建代码缘由、引入提交、作者和 companion files 时 | 与文档 Skill 交集；来源/删除风险未完全确认 | 未检出 | REVIEW |
| `context-engineering` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 开始/恢复会话、切换任务、交接、压缩或排查上下文漂移时 | 被规格 Skill 使用；与工作集检索互补 | 80 | KEEP |
| `debugging-and-error-recovery` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 测试失败、构建破坏、异常行为或意外错误需要系统化根因调试时 | 被 CI/CD Skill 引用；与图谱调试互补 | 164 | KEEP |
| `dependency-doctor` | 项目未跟踪；author Matt Van Horn | 检查 requirements/pyproject/package manifest 的直接依赖陷阱时 | 与安全/迁移 Skill 交集有限；来源/删除风险未完全确认 | 未检出 | REVIEW |
| `deprecation-and-migration` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 移除旧系统、API 或功能，及用户迁移方案设计时 | 与 API、发布 Skill 互补 | 未检出 | KEEP |
| `documentation-and-adrs` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 架构决策、公共 API、发布特性或需要长期上下文记录时 | 与文档质询、历史考古有交集但输出目标不同 | 未检出 | KEEP |
| `doubt-driven-development` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 正确性优先、陌生代码或高风险非平凡决策需要独立反证时 | 引用通用审查；与文档质询交集；强制额外审查与本机“禁止繁琐审核”有张力 | 4 | REVIEW |
| `frontend-ui-engineering` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 创建/修改生产级用户界面、组件、布局、状态或交互时 | 与 Hallmark、浏览器测试互补 | 未检出 | KEEP |
| `git-workflow-and-versioning` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 任意代码变更的提交、分支、冲突和版本工作流时 | 引用通用审查；受项目 AGENTS.md 约束 | 未检出 | KEEP |
| `idea-refine` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 用户明确说 `idea-refine`/`ideate`，需要结构化发散与收敛时 | 无完整替代；显式触发范围窄但清晰 | 未检出 | KEEP |
| `incremental-implementation` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 多文件或较大变更需要分阶段实现和验收时 | 被规格 Skill 使用；与任务拆解互补 | 未检出 | KEEP |
| `performance-optimization` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 有性能要求、回归、CWV 或已定位瓶颈时 | 与浏览器测试交集但不限浏览器 | 未检出 | KEEP |
| `planning-and-task-breakdown` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 有明确规格，需要拆成有序可实施任务时 | 与 `planning-with-files` 互补；不要求跨 Session | 未检出 | KEEP |
| `scope-creep-detector` | 项目未跟踪；author Matt Van Horn | 检查 diff 是否超出当前意图、引入无关文件或依赖时 | 与变更审查交集；来源/删除风险未完全确认 | 未检出 | REVIEW |
| `security-and-hardening` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 用户输入、认证、存储、外部集成或不可信数据需要安全加固时 | 与 API、依赖检查 Skill 互补 | 未检出 | KEEP |
| `shipping-and-launch` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 生产发布、监控、分阶段 rollout、回滚或上线检查时 | 与 CI/CD 互补 | 未检出 | KEEP |
| `source-driven-development` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 实现决定需要官方文档、来源引用和避免过时模式时 | 与联网研究/OpenAI 文档有交集，但官方来源约束更窄更强 | 26 | KEEP |
| `spec-driven-development` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 新项目、重大功能或需求不清且缺少规格时 | 使用上下文、增量实现、TDD 三项；无完整替代 | 未检出 | KEEP |
| `test-driven-development` | `addyosmani/agent-skills`（锁文件）；自定义状态未核实 | 实现逻辑、修 bug 或改变行为，需要测试先行和回归证明时 | 替代失效全局 `tdd`；引用浏览器测试；受项目硬规则约束 | 423 | KEEP |
| `using-agent-skills` | `addyosmani/agent-skills`（锁文件）；是 | 每个任务开始、用户点名 Skill 或需要从运行时目录选择 Skill 时 | 运行时 Skill discovery 基础层；禁止自动删除 | 454 | KEEP |
| `watch` | 项目未跟踪；`bradautomates/claude-video` 0.2.0 | 用户给出 URL/本地视频并要求字幕、帧或内容理解时 | 与 `video-use` 交集；来源/删除风险未完全确认 | 未检出 | REVIEW |

## REMOVE

只清理已证实失效的入口，不删除实际 Skill 内容：

| 路径 | 证据 | 安全动作 | 状态 |
|---|---|---|---|
| `/home/gnen/.codex/skills/diagnosing-bugs` | 符号链接目标不存在；项目提交 `4c0b2b1` 明确由 `debugging-and-error-recovery` 替代；运行时未发现 | 使用精确链接回收站动作，不递归删除 | 已完成 |
| `/home/gnen/.codex/skills/tdd` | 符号链接目标不存在；项目提交 `4c0b2b1` 明确由 `test-driven-development` 替代；运行时未发现 | 使用精确链接回收站动作，不递归删除 | 已完成 |

没有根据“看起来不常用”删除任何可运行 Skill。

## REVIEW

- `archify`、`commit-archaeologist`、`dependency-doctor`、`scope-creep-detector`、`watch`：项目层可发现但未被 Git 跟踪；来源、更新渠道和删除风险未完全确认。
- `browser-testing-with-devtools`：项目已跟踪且被 TDD 引用，但声明的 Chrome DevTools MCP 未出现在当前配置；不能据此自动删除或安装 MCP。
- `doubt-driven-development`：有真实显式读取记录，但额外审查步骤与全局“禁止繁琐流程”规则存在冲突；应在需要高风险审查时按最小范围使用。
- `/home/gnen/.codex/skills/.system/review-agent/SKILL.md`：系统目录存在但没有进入当前会话的 Available skills；系统项不可自动删除。
- 10 个 Wigolo 子路由：`wigolo-agent`、`wigolo-cache`、`wigolo-crawl`、`wigolo-diff`、`wigolo-extract`、`wigolo-fetch`、`wigolo-find-similar`、`wigolo-research`、`wigolo-search`、`wigolo-watch`。它们在 `config.toml` 中显式禁用以节省重复路由 metadata，但 `wigolo/SKILL.md` 正文仍引用其文件；不能删除。
- `grill-with-docs`、`instructor` 和 Obsidian 五项在用户目录以有效符号链接暴露，实际内容分别来自共享 Agent/Obsidian upstream；这是同一物理 Skill 的多路径，不应重复清理。
- marketplace snapshot、Skill backup/staging、仓库 `books/`、`deliverables/`、普通文档和测试 fixtures 不在运行时 Skill roots，不算安装项。

## 候选 Skill 比较（清理后）

| 候选 | 当前状态 | 能力比较 | Codex/触发与稳定性判断 | 决定 |
|---|---|---|---|---|
| `source-driven-development` | 当前项目已存在，可自动触发；显式下限 26 | 联网研究/平台 Skill 更广；本项对官方来源约束更直接，属于部分重叠 | 当前项目路径已被 Codex 发现；再装用户级副本会制造重复路由 | **SKIP**（已存在，不重复安装） |
| `doubt-driven-development` | 当前项目已存在；显式下限 4 | 与通用审查、文档质询部分重叠；独立反证角度仍有差异 | 额外流程与本机简洁规则有张力，需人工取舍 | **SKIP**（已存在，不额外安装） |
| `context-engineering` | 当前项目已存在；显式下限 80 | 与工作集检索互补；被规格 Skill 使用，不是完整重复 | 项目层可自动发现，且已有依赖 | **SKIP**（已存在，不额外安装） |
| `debugging-and-error-recovery` | 当前项目已存在；显式下限 164 | 与图谱调试互补：本项是通用根因流程，图谱项负责导航 | 被 CI/CD Skill 引用；项目层触发可用 | **SKIP**（已存在，不额外安装） |
| `browser-testing-with-devtools` | 当前项目已存在；显式下限 101 | 与前端、性能和 TDD 有交集，但提供浏览器真实运行验证 | Chrome DevTools MCP 尚未确认；保留 REVIEW，不重复安装或擅自补依赖 | **SKIP**（已存在，不额外安装） |
| `code-review-and-quality` | 当前项目已存在；显式下限 202 | 图谱套件覆盖影响分析/PR/delta；本项覆盖通用多轴审查，不能证明完整覆盖 | 已有项目层自动触发，且被其他项目 Skill 引用 | **SKIP**（已存在，不额外安装） |

现有项目副本的 Codex 兼容性和自动触发已满足当前仓库；新增全局同名副本不会提高功能覆盖，反而增加上下文 metadata、优先级歧义和维护分叉。维护活跃度因未联网核验不作为删除依据；稳定性以当前项目已跟踪、已有依赖和历史显式调用为主要本地证据。

## 最终分类汇总

### KEEP

85 个当前可发现 Skill 中，**78 个 KEEP、7 个 REVIEW**。KEEP 包括 5 个系统 Skill、45 个用户 Codex Skill、8 个共享 Agent Skill和项目层 20 个。

### REMOVE

2 个失效符号链接入口：`diagnosing-bugs`、`tdd`。实际 Skill 内容没有被删除。

### REVIEW

当前可发现的 7 个 REVIEW：5 个未跟踪项目 Skill、`browser-testing-with-devtools`、`doubt-driven-development`。另外保留隐藏系统 `review-agent` 和 10 个禁用 Wigolo 子路由，不把它们当作可删除候选。

### INSTALL

**无**。6 个候选均已在当前项目中发现，不需要安装同名全局副本。

### SKIP

6 个候选全部 SKIP，理由是“已存在且额外安装会重复”，不是“候选没有价值”。

## 依赖与基础设施证据

- `build-graph`、`explore-codebase`、`debug-issue`、`refactor-safely`、`review-changes`、`review-delta`、`review-pr` 使用已配置的 `code-review-graph` MCP。
- GitNexus 三项使用已配置的 GitNexus MCP。
- `video-use` 内嵌 `manim-video`；小红书工作流调用 `video-shotcraft`；`animate` 与 `review-animations` 配套。
- 规格 Skill 使用 `context-engineering`、增量实现、TDD；CI/CD 引用通用调试；TDD 引用浏览器测试；Git 工作流和对抗审查引用通用代码审查。
- 基础路由、用户自定义、系统、Plugin/Skill 管理器和项目明确依赖项均未自动删除。

## 验证记录

1. `codex plugin list --json`：`installed=[]`、`available=[]`。
2. `codex plugin marketplace list`：只有 `openai-api-curated` 缓存目录。
3. 当前项目运行时目录清单：85 项（5 + 45 + 8 + 27）。
4. 断链复核：`diagnosing-bugs`、`tdd` 两条路径均不存在。
5. `codex-skill-budget` 审计：`skill_count=85`、`disabled_skill_count=10`、`listing_tokens=5436`、`truncation_count=55`、`proposed_disables=[]`、退出码 2；没有策略支持的安全禁用候选，因此没有自动停用更多路由。
6. 历史会话审计：扫描 534 个文件，排除当前会话后的历史 527 个文件；51404 次工具调用；4083 次包含 `SKILL.md` 读取模式的调用。上述 Skill 次数仅是显式读取下限，不是完整自动触发统计。

## Residual Risks

- 55 个简介截断可能降低自动触发准确性；预算工具没有提供可安全应用的重复路由候选，不能为压缩上下文而删除 Skill。
- 当前统计只覆盖本项目运行时；换项目后项目层 27 项不会等价于全局安装。
- `browser-testing-with-devtools` 的 Chrome DevTools MCP 依赖尚未在当前配置中确认。
- 5 个未跟踪项目 Skill 的来源和维护路径需要用户后续确认；在此之前保持 REVIEW。
- 未联网核验 upstream 最新维护活跃度、许可证或版本，因此没有据此做删除决定。
