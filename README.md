<div align="center">

# 🤖 Free Claude Code

### Use Claude Code CLI & VSCode for free. No Anthropic API key required.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776ab.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=for-the-badge)](https://github.com/astral-sh/uv)
[![Tested with Pytest](https://img.shields.io/badge/testing-Pytest-00c0ff.svg?style=for-the-badge)](https://github.com/Alishahryar1/free-claude-code/actions/workflows/tests.yml)
[![Type checking: Ty](https://img.shields.io/badge/type%20checking-ty-ffcc00.svg?style=for-the-badge)](https://pypi.org/project/ty/)
[![Code style: Ruff](https://img.shields.io/badge/code%20formatting-ruff-f5a623.svg?style=for-the-badge)](https://github.com/astral-sh/ruff)
[![Logging: Loguru](https://img.shields.io/badge/logging-loguru-4ecdc4.svg?style=for-the-badge)](https://github.com/Delgan/loguru)

A lightweight proxy that routes Claude Code's Anthropic API calls to **MiniMax** or **DeepSeek**.

[Quick Start](#quick-start) · [Providers](#providers) · [Discord Bot](#discord-bot) · [Configuration](#configuration) · [Development](#development) · [Contributing](#contributing)

---

</div>

<div align="center">
  <img src="pic.png" alt="Free Claude Code in action" width="700">
  <p><em>Claude Code running through an alternative model provider</em></p>
</div>

## Features

| Feature                    | Description                                                                                     |
| -------------------------- | ----------------------------------------------------------------------------------------------- |
| **Alternative Providers**  | Route Claude Code through MiniMax or DeepSeek APIs                                               |
| **Drop-in Replacement**    | Set 2 env vars. No modifications to Claude Code CLI or VSCode extension needed                  |
| **2 Providers**            | MiniMax and DeepSeek                                                                             |
| **Per-Model Mapping**      | Route Opus / Sonnet / Haiku to different models and providers. Mix providers freely             |
| **Thinking Token Support** | Parses `<think>` tags and `reasoning_content` into native Claude thinking blocks                |
| **Heuristic Tool Parser**  | Models outputting tool calls as text are auto-parsed into structured tool use                   |
| **Request Optimization**   | 5 categories of trivial API calls intercepted locally, saving quota and latency                 |
| **Smart Rate Limiting**    | Proactive rolling-window throttle + reactive 429 exponential backoff + optional concurrency cap |
| **Discord / Telegram Bot** | Remote autonomous coding with tree-based threading, session persistence, and live progress      |
| **Subagent Control**       | Task tool interception forces `run_in_background=False`. No runaway subagents                   |
| **Extensible**             | Clean `BaseProvider` and `MessagingPlatform` ABCs. Add new providers or platforms easily        |

## Quick Start

### Prerequisites

1. Get an API key:
   - **MiniMax**: [platform.minimax.io](https://platform.minimax.io)
   - **DeepSeek**: [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
2. Install [Claude Code](https://github.com/anthropics/claude-code)

### Install `uv`
```bash
# Install uv (required to run the project)
pip install uv
```
If uv is already installed, run uv self update to get the latest version.

### Clone & Configure

```bash
git clone https://github.com/Alishahryar1/free-claude-code.git
cd free-claude-code
cp .env.example .env
```

Choose your provider and edit `.env`:

<details>
<summary><b>MiniMax</b></summary>

```dotenv
MINIMAX_API_KEY="your-minimax-key-here"
MINIMAX_BASE_URL="https://api.minimaxi.com/anthropic"

MODEL_OPUS=
MODEL_SONNET=
MODEL_HAIKU=
MODEL="minimax/MiniMax-M3"                         # fallback

# Global switch for provider reasoning requests and Claude thinking blocks.
ENABLE_THINKING=true
```

</details>

<details>
<summary><b>DeepSeek</b> (direct API)</summary>

```dotenv
DEEPSEEK_API_KEY="your-deepseek-key-here"

MODEL_OPUS="deepseek/deepseek-reasoner"
MODEL_SONNET="deepseek/deepseek-chat"
MODEL_HAIKU="deepseek/deepseek-chat"
MODEL="deepseek/deepseek-chat"                      # fallback
```

</details>

<details>
<summary><b>Mix providers</b></summary>

Each `MODEL_*` variable can use a different provider. `MODEL` is the fallback for unrecognized Claude models.

```dotenv
MINIMAX_API_KEY="your-minimax-key-here"
DEEPSEEK_API_KEY="your-deepseek-key-here"

MODEL_OPUS="minimax/MiniMax-M3"
MODEL_SONNET="deepseek/deepseek-chat"
MODEL_HAIKU="deepseek/deepseek-chat"
MODEL="minimax/MiniMax-M3"                         # fallback
```

</details>

> Migration: `NIM_ENABLE_THINKING` was removed in this release. Rename it to `ENABLE_THINKING`.

<details>
<summary><b>Optional Authentication</b> (restrict access to your proxy)</summary>

Set `ANTHROPIC_AUTH_TOKEN` in `.env` to require clients to authenticate:

```dotenv
ANTHROPIC_AUTH_TOKEN="your-secret-token-here"
```

**How it works:**
- If `ANTHROPIC_AUTH_TOKEN` is empty (default), no authentication is required (backward compatible)
- If set, clients must provide the same token via the `ANTHROPIC_AUTH_TOKEN` header
- The `claude-pick` script automatically reads the token from `.env` if configured

**Example usage:**
```bash
# With authentication
ANTHROPIC_AUTH_TOKEN="your-secret-token-here" \
ANTHROPIC_BASE_URL="http://localhost:8082" claude

# claude-pick automatically uses the configured token
claude-pick
```

Use this feature if:
- Running the proxy on a public network
- Sharing the server with others but restricting access
- Wanting an additional layer of security

</details>

### Run It

**Terminal 1:** Start the proxy server:

```bash
uv run uvicorn server:app --host 127.0.0.1 --port 8082
```

**Terminal 2:** Run Claude Code:

Point `ANTHROPIC_BASE_URL` at the proxy root URL, not `http://localhost:8082/v1`.

#### Powershell
```powershell
$env:ANTHROPIC_AUTH_TOKEN="freecc"; $env:ANTHROPIC_BASE_URL="http://localhost:8082"; claude
```
#### Bash
```bash
ANTHROPIC_AUTH_TOKEN="freecc" ANTHROPIC_BASE_URL="http://localhost:8082" claude
```

That's it! Claude Code now uses your configured provider for free.

<details>
<summary><b>VSCode Extension Setup</b></summary>

1. Start the proxy server (same as above).
2. Open Settings (`Ctrl + ,`) and search for `claude-code.environmentVariables`.
3. Click **Edit in settings.json** and add:

```json
"claudeCode.environmentVariables": [
  { "name": "ANTHROPIC_BASE_URL", "value": "http://localhost:8082" },
  { "name": "ANTHROPIC_AUTH_TOKEN", "value": "freecc" }
]
```

4. Reload extensions.
5. **If you see the login screen**: Click **Anthropic Console**, then authorize. The extension will start working. You may be redirected to buy credits in the browser; ignore it — the extension already works.

To switch back to Anthropic models, comment out the added block and reload extensions.

</details>


<details>
<summary><b>IntelliJ Extension Setup</b></summary>

1. Open the configuration file:
   - **Windows**: `C:\Users\%USERNAME%\AppData\Roaming\JetBrains\acp-agents\installed.json`
   - **Linux/macOS**: `~/.jetbrains/acp.json`
2. Inside acp.registry.claude-acp, change:

   ```
   "env": {}
   ```
   to

   ```
   "env": {
   "ANTHROPIC_AUTH_TOKEN": "freecc",
   "ANTHROPIC_BASE_URL": "http://localhost:8082"
   }
   ```
3. Start the proxy server
4. Restart IDE

</details>

<details>
<summary><b>Configured Model Launcher</b></summary>

`claude-pick` keeps its historical name but now launches Claude using the
`MODEL` configured in `.env`. Add an alias to `~/.zshrc` or `~/.bashrc`:

```bash
alias claude-pick="/absolute/path/to/free-claude-code/claude-pick"
```

Then reload your shell (`source ~/.zshrc` or `source ~/.bashrc`) and run `claude-pick`.

</details>

### Install as a Package (no clone needed)

```bash
uv tool install git+https://github.com/Alishahryar1/free-claude-code.git
fcc-init        # creates ~/.config/free-claude-code/.env from the built-in template
```

Edit `~/.config/free-claude-code/.env` with your API keys and model names, then:

```bash
free-claude-code    # starts the server
```

> To update: `uv tool upgrade free-claude-code`

---

## How It Works

```
┌─────────────────┐        ┌──────────────────────┐        ┌──────────────────┐
│  Claude Code    │───────>│  Free Claude Code    │───────>│  LLM Provider    │
│  CLI / VSCode   │<───────│  Proxy (:8082)       │<───────│ MiniMax/DeepSeek │
└─────────────────┘        └──────────────────────┘        └──────────────────┘
   Anthropic API                                             Provider API
   format (SSE)                                              format (SSE)
```

- **Transparent proxy**: Claude Code sends standard Anthropic API requests; the proxy forwards them to your configured provider
- **Per-model routing**: Opus / Sonnet / Haiku requests resolve to their model-specific backend, with `MODEL` as fallback
- **Request optimization**: 5 categories of trivial requests (quota probes, title generation, prefix detection, suggestions, filepath extraction) are intercepted and responded to locally without using API quota
- **Format handling**: Requests use each provider's native compatible format and stream back as Anthropic SSE
- **Thinking tokens**: `<think>` tags and `reasoning_content` fields are converted into native Claude thinking blocks when `ENABLE_THINKING=true`

The proxy also exposes Claude-compatible probe routes: `GET /v1/models`, `POST /v1/messages`, `POST /v1/messages/count_tokens`, plus `HEAD`/`OPTIONS` support for the common probe endpoints.

---

## Providers

| Provider     | Cost        | Rate Limit | Best For                              |
| ------------ | ----------- | ---------- | ------------------------------------- |
| **MiniMax**  | Usage-based | Varies     | Anthropic-compatible MiniMax models   |
| **DeepSeek** | Usage-based | Varies     | Direct DeepSeek chat/reasoner access  |

Models use a prefix format: `provider_prefix/model/name`. An invalid prefix causes an error.

| Provider | `MODEL` prefix | API Key Variable   | Default Base URL                 |
| -------- | -------------- | ------------------ | -------------------------------- |
| MiniMax  | `minimax/...`  | `MINIMAX_API_KEY`  | `api.minimaxi.com/anthropic`     |
| DeepSeek | `deepseek/...` | `DEEPSEEK_API_KEY` | `api.deepseek.com`               |

<details>
<summary><b>MiniMax models</b></summary>

- `minimax/MiniMax-M3`

Browse: [platform.minimax.io](https://platform.minimax.io)

</details>

<details>
<summary><b>DeepSeek models</b></summary>

DeepSeek currently exposes the direct API models:

- `deepseek/deepseek-chat`
- `deepseek/deepseek-reasoner`

Browse: [api-docs.deepseek.com](https://api-docs.deepseek.com)

</details>

### DeepSeek Harness plugins

The optional DeepSeek Harness route runs the official Cordis graph behind a
JSON-RPC subprocess and projects its events to Claude-compatible SSE. The
sandbox-backed graph uses the pinned Node closure in `harness/runtime`; install
it once before enabling DSH:

```bash
cd harness/runtime
npm ci
```

The rc.6 single-file runtime is not selected because its closed module resolver
cannot load the sandbox providers from this external graph. Configure DSH with:

```dotenv
DSH_ENABLED=true
MODEL="dsh/deepseek/deepseek-chat"
DEEPSEEK_API_KEY="your-deepseek-key-here"
# Empty selects the official full plugin allowlist.
DSH_PLUGIN_ALLOWLIST=""
```

The default `harness/runtime/cordis.yml` includes the JSON-RPC server, agent
spine, DeepSeek LLM, JSONL persistence/checkpoints, subprocess, bash and
filesystem plugins. It contains no credentials; the bridge passes
`DEEPSEEK_API_KEY` and optional `DEEPSEEK_BASE_URL` only to the runtime child.

The repository selects the pinned Node launcher automatically. To override it,
set an explicit argv command (the bridge does not invoke a shell):

```dotenv
DSH_RUNTIME_COMMAND="node /absolute/path/to/free-claude-code/harness/runtime/runner.mjs"
DSH_CORDIS_CONFIG="/absolute/path/to/free-claude-code/harness/runtime/cordis.yml"
```

Custom Cordis configs must retain the JSON-RPC server and list every configured
plugin in `DSH_PLUGIN_ALLOWLIST`. The route can also be selected per request
with `dsh/<provider>/<model>`. Check `GET /v1/harness/status` before sending
traffic; missing runtime dependencies or invalid Cordis configuration are
reported as an actionable 503 instead of a misleading successful stream.

---

## Discord Bot

Control Claude Code remotely from Discord (or Telegram). Send tasks, watch live progress, and manage multiple concurrent sessions.

**Capabilities:**

- Tree-based message threading: reply to a message to fork the conversation
- Session persistence across server restarts
- Live streaming of thinking tokens, tool calls, and results
- Unlimited concurrent Claude CLI sessions (concurrency controlled by `PROVIDER_MAX_CONCURRENCY`)
- Voice notes: send voice messages; they are transcribed and processed as regular prompts
- Commands: `/stop` (cancel a task; reply to a message to stop only that task), `/clear` (reset all sessions, or reply to clear a branch), `/stats`

### Setup

1. **Create a Discord Bot**: Go to [Discord Developer Portal](https://discord.com/developers/applications), create an application, add a bot, and copy the token. Enable **Message Content Intent** under Bot settings.

2. **Edit `.env`:**

```dotenv
MESSAGING_PLATFORM="discord"
DISCORD_BOT_TOKEN="your_discord_bot_token"
ALLOWED_DISCORD_CHANNELS="123456789,987654321"
```

> Enable Developer Mode in Discord (Settings → Advanced), then right-click a channel and "Copy ID". Comma-separate multiple channels. If empty, no channels are allowed.

3. **Configure the workspace** (where Claude will operate):

```dotenv
CLAUDE_WORKSPACE="./agent_workspace"
ALLOWED_DIR="C:/Users/yourname/projects"
```

4. **Start the server:**

```bash
uv run uvicorn server:app --host 127.0.0.1 --port 8082
```

5. **Invite the bot** via OAuth2 URL Generator (scopes: `bot`, permissions: Read Messages, Send Messages, Manage Messages, Read Message History).

### Telegram

Set `MESSAGING_PLATFORM=telegram` and configure:

```dotenv
TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
ALLOWED_TELEGRAM_USER_ID="your_telegram_user_id"
```

Get a token from [@BotFather](https://t.me/BotFather); find your user ID via [@userinfobot](https://t.me/userinfobot).

### Selecting Claude Code or Codex CLI

The messaging adapter runs one local CLI backend per server process. Keep the
safe defaults below for a read-only phone control surface:

```dotenv
AGENT_BACKEND="codex"          # or "claude"
AGENT_PERMISSION_MODE="plan"   # Claude only; never bypasses permissions by default
CLAUDE_AUTH_MODE="local"       # local Claude OAuth; use "proxy" for this repo's provider proxy
CODEX_BIN="codex"
CODEX_MODEL=""                 # optional Codex model override
CODEX_SANDBOX="read-only"      # read-only | workspace-write | danger-full-access
CODEX_APPROVAL_REQUIRED="true" # stage write-capable Codex runs until /approve
ALLOWED_DIR="/home/you/projects/my-repo"
```

`ALLOWED_DIR` is the only workspace exposed to the messaging process. Run the
server in WSL and keep the bot in polling mode; no inbound WSL port is needed
for Telegram. `CODEX_SANDBOX=read-only` and Claude `plan` mode are intentional
safe defaults. Enabling a write-capable mode is an operator decision and should
be paired with an external diff/approval workflow until the structured approval
adapter is enabled.

### Protocol-level low-risk approval hook

`fcc-approval-hook` is an opt-in, single-shot hook for Claude `PreToolUse` and
Codex `PreToolUse` / `PermissionRequest` events. It auto-approves only the
built-in read-only tool set and explicitly allowlisted command prefixes. It
returns no output for unknown, compound, out-of-workspace, or otherwise
ambiguous requests, so the CLI keeps its normal approval prompt. Destructive
commands are denied by the local policy.

The hook is disabled by default. Enable it only in a hook configuration that
also sets an explicit workspace boundary:

```bash
FCC_APPROVAL_ENABLED=true \
FCC_APPROVAL_WORKSPACES=/absolute/path/to/repo \
FCC_APPROVAL_SCOPE=once \
fcc-approval-hook
```

Register the executable through the hook mechanism documented by the CLI you
use. For Claude Code, add it to the `PreToolUse` hook in
`~/.claude/settings.json`; Codex supports the same executable through its
`PreToolUse` and `PermissionRequest` hook configuration. Keep the command
allowlist narrow, and do not enable permanent approvals unless the explicit
`FCC_APPROVAL_ALLOW_PERMANENT=true` setting and a permanent scope are both
required by your workflow. The hook never executes the command from the input
payload and never records the command or credentials in a log.

### Voice Notes

Send voice messages on Discord or Telegram; they are transcribed and processed as regular prompts.

Voice notes use local [Hugging Face Whisper](https://huggingface.co/openai/whisper-large-v3-turbo), with CPU and CUDA support.

**Install the voice extras:**

```bash
# If you cloned the repo:
uv sync --extra voice_local

# If you installed as a package (no clone):
uv tool install "free-claude-code[voice_local] @ git+https://github.com/Alishahryar1/free-claude-code.git"
```

Configure via `WHISPER_DEVICE` (`cpu` | `cuda`) and `WHISPER_MODEL`. See the [Configuration](#configuration) table for all voice variables and supported model values.

---

## Configuration

### Core

| Variable             | Description                                                           | Default                                           |
| -------------------- | --------------------------------------------------------------------- | ------------------------------------------------- |
| `MODEL`              | Fallback model (`provider/model/name` format; invalid prefix → error) | `minimax/MiniMax-M3`                              |
| `MODEL_OPUS`         | Model for Claude Opus requests; empty falls back to `MODEL`           | empty                                             |
| `MODEL_SONNET`       | Model for Claude Sonnet requests; empty falls back to `MODEL`         | empty                                             |
| `MODEL_HAIKU`        | Model for Claude Haiku requests; empty falls back to `MODEL`          | empty                                             |
| `ENABLE_THINKING`    | Global switch for provider reasoning requests and Claude thinking blocks. Set `false` to hide thinking across all providers. | `true` |
| `MINIMAX_API_KEY`    | MiniMax API key                                                       | required for MiniMax                              |
| `MINIMAX_BASE_URL`   | MiniMax Anthropic-compatible endpoint                                 | `https://api.minimaxi.com/anthropic`              |
| `DEEPSEEK_API_KEY`   | DeepSeek API key                                                      | required for DeepSeek                             |
| `DEEPSEEK_BASE_URL`  | Optional DeepSeek-compatible endpoint for DSH                         | empty (official default)                          |
| `MINIMAX_PROXY`      | Optional proxy URL for MiniMax requests                               | `""`                                             |

### Rate Limiting & Timeouts

| Variable                   | Description                               | Default |
| -------------------------- | ----------------------------------------- | ------- |
| `PROVIDER_RATE_LIMIT`      | LLM API requests per window               | `40`    |
| `PROVIDER_RATE_WINDOW`     | Rate limit window (seconds)               | `60`    |
| `PROVIDER_MAX_CONCURRENCY` | Max simultaneous open provider streams    | `5`     |
| `HTTP_READ_TIMEOUT`        | Read timeout for provider requests (s)    | `120`   |
| `HTTP_WRITE_TIMEOUT`       | Write timeout for provider requests (s)   | `10`    |
| `HTTP_CONNECT_TIMEOUT`     | Connect timeout for provider requests (s) | `2`     |

### Messaging & Voice

| Variable                   | Description                                                                                                                                                        | Default             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| `MESSAGING_PLATFORM`       | `discord` or `telegram`                                                                                                                                            | `discord`           |
| `DISCORD_BOT_TOKEN`        | Discord bot token                                                                                                                                                  | `""`                |
| `ALLOWED_DISCORD_CHANNELS` | Comma-separated channel IDs (empty = none allowed)                                                                                                                 | `""`                |
| `TELEGRAM_BOT_TOKEN`       | Telegram bot token                                                                                                                                                 | `""`                |
| `ALLOWED_TELEGRAM_USER_ID` | Allowed Telegram user ID                                                                                                                                           | `""`                |
| `AGENT_BACKEND`            | Local CLI backend: `claude` or `codex`                                                                                                                             | `claude`            |
| `AGENT_PERMISSION_MODE`    | Claude mode: `plan`, `acceptEdits`, `auto`, or `bypassPermissions`                                                                                                 | `plan`              |
| `CLAUDE_AUTH_MODE`         | Claude credentials: `proxy` (provider proxy) or `local` (`claude auth login`)                                                                                       | `proxy`             |
| `CODEX_BIN`                | Codex executable path/name                                                                                                                                          | `codex`             |
| `CODEX_MODEL`              | Optional Codex model override                                                                                                                                       | empty               |
| `CODEX_SANDBOX`            | Codex sandbox: `read-only`, `workspace-write`, or `danger-full-access`                                                                                              | `read-only`         |
| `CODEX_APPROVAL_REQUIRED`  | Stage Codex write-capable runs until an operator sends `/approve`                                                                                                    | `true`              |
| `CLI_AUTO_APPROVAL_ENABLED` | Enable the protocol-level low-risk approval hook (opt-in)                                                                                                             | `false`             |
| `CLI_AUTO_APPROVAL_SCOPE` | Maximum automatic approval scope: `once`, `session`, or `permanent`                                                                                                  | `once`              |
| `CLI_AUTO_APPROVAL_COMMANDS` | Comma-separated command prefixes; empty uses the built-in read-only set                                                                                              | empty               |
| `CLI_AUTO_APPROVAL_WORKSPACES` | Comma-separated absolute workspace roots; empty uses the current manager workspace                                                                                  | empty               |
| `CLI_AUTO_APPROVAL_ALLOW_PERMANENT` | Allow permanent-scope decisions when explicitly requested                                                                                                           | `false`             |
| `CLAUDE_WORKSPACE`         | Directory where the agent operates                                                                                                                                 | `./agent_workspace` |
| `ALLOWED_DIR`              | Allowed directories for the agent                                                                                                                                  | `""`                |
| `MESSAGING_RATE_LIMIT`     | Messaging messages per window                                                                                                                                      | `1`                 |
| `MESSAGING_RATE_WINDOW`    | Messaging window (seconds)                                                                                                                                         | `1`                 |
| `VOICE_NOTE_ENABLED`       | Enable voice note handling                                                                                                                                         | `true`              |
| `WHISPER_DEVICE`           | `cpu` \| `cuda`                                                                                                                                                     | `cpu`               |
| `WHISPER_MODEL`            | Whisper model (`tiny`/`base`/`small`/`medium`/`large-v2`/`large-v3`/`large-v3-turbo`, or a Hugging Face model ID)                                               | `base`              |
| `HF_TOKEN`                 | Hugging Face token for faster downloads (local Whisper, optional)                                                                                                  | —                   |

<details>
<summary><b>Advanced: Request optimization flags</b></summary>

These are enabled by default and intercept trivial Claude Code requests locally to save API quota.

| Variable                          | Description                    | Default |
| --------------------------------- | ------------------------------ | ------- |
| `FAST_PREFIX_DETECTION`           | Enable fast prefix detection   | `true`  |
| `ENABLE_NETWORK_PROBE_MOCK`       | Mock network probe requests    | `true`  |
| `ENABLE_TITLE_GENERATION_SKIP`    | Skip title generation requests | `true`  |
| `ENABLE_SUGGESTION_MODE_SKIP`     | Skip suggestion mode requests  | `true`  |
| `ENABLE_FILEPATH_EXTRACTION_MOCK` | Mock filepath extraction       | `true`  |

</details>

See [`.env.example`](.env.example) for all supported parameters.

---

## Development

### Project Structure

```
free-claude-code/
├── server.py              # Entry point
├── api/                   # FastAPI routes, request detection, optimization handlers
├── providers/             # BaseProvider, MiniMax, DeepSeek, and shared provider utilities
│   └── common/            # Shared utils (SSE builder, message converter, parsers, error mapping)
├── messaging/             # MessagingPlatform ABC + Discord/Telegram bots, session management
├── config/                # Settings and logging
├── cli/                   # CLI session and process management
└── tests/                 # Pytest test suite
```

### Commands

```bash
uv run ruff format     # Format code
uv run ruff check      # Lint
uv run ty check        # Type checking
uv run pytest          # Run tests
```

### Extending

**Adding an OpenAI-compatible provider** (Groq, Together AI, etc.) — extend `OpenAICompatibleProvider`:

```python
from providers.openai_compat import OpenAICompatibleProvider
from providers.base import ProviderConfig

class MyProvider(OpenAICompatibleProvider):
    def __init__(self, config: ProviderConfig):
        super().__init__(config, provider_name="MYPROVIDER",
                         base_url="https://api.example.com/v1", api_key=config.api_key)
```

**Adding a fully custom provider** — extend `BaseProvider` directly and implement `stream_response()`.

**Adding a messaging platform** — extend `MessagingPlatform` in `messaging/` and implement `start()`, `stop()`, `send_message()`, `edit_message()`, and `on_message()`.

---

## Contributing

- Report bugs or suggest features via [Issues](https://github.com/Alishahryar1/free-claude-code/issues)
- Add new LLM providers (Groq, Together AI, etc.)
- Add new messaging platforms (Slack, etc.)
- Improve test coverage
- Not accepting Docker integration PRs for now

```bash
git checkout -b my-feature
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest
# Open a pull request
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

Built with [FastAPI](https://fastapi.tiangolo.com/), [OpenAI Python SDK](https://github.com/openai/openai-python), [discord.py](https://github.com/Rapptz/discord.py), and [python-telegram-bot](https://python-telegram-bot.org/).
