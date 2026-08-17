# DeepSeek Harness runtime

The Python application starts the pinned `0.1.0-rc.6` JSON-RPC launcher in this
directory through `runner.mjs`. The rc.6 single-file runtime cannot resolve the
sandbox provider packages from an external Cordis graph, so it is not used for
this sandbox-backed configuration.

Install the complete pinned plugin closure once from this directory:

```sh
npm ci
```

The bundled `cordis.yml` is the full official agent graph: JSON-RPC server,
agent spine, DeepSeek LLM, JSONL persistence/checkpoints, subprocess, bash and
filesystem plugins. It is secret-free; credentials are passed through the
child process environment (`DEEPSEEK_API_KEY` and optional
`DEEPSEEK_BASE_URL`). Every plugin in a custom Cordis config must also appear
in `DSH_PLUGIN_ALLOWLIST`.

The repository selects this command by default when DSH is enabled. To make it
explicit in a deployment, set an argv command in the project environment (the
bridge never invokes a shell string):

```dotenv
DSH_ENABLED=true
DSH_RUNTIME_COMMAND="node /absolute/path/to/free-claude-code/harness/runtime/runner.mjs"
DSH_RUNTIME_ARGS=""
DSH_CORDIS_CONFIG="/absolute/path/to/free-claude-code/harness/runtime/cordis.yml"
```

Omit `DSH_RUNTIME_COMMAND` to use the repository's pinned Node launcher.
