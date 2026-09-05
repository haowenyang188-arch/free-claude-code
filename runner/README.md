# Local Scraper Runner

The local runner is the execution layer between the hosted control page and the
real WSL scraper. The supported Windows launcher starts the verified
`local_runner.py` service on `https://127.0.0.1:8790`.

## Start

From Windows, double-click `start_runner.bat`. The script keeps the runner in
the foreground so closing the window also stops its owned child processes. Set
`WB_RUNNER_DIR` only when the verified runner directory is elsewhere.

For the standalone WSL implementation in this repository:

```bash
uv run uvicorn runner.server:app --host 127.0.0.1 --port 8790
```

The default scraper root is `/home/gnen/scraper`. Override it only with the
absolute `RUNNER_SCRAPER_ROOT` environment variable. Chrome is started through
the fixed scripts in `RUNNER_CHROME_SCRIPT_DIR` (default:
`/mnt/c/Users/gnen0/Desktop/抓取工具`). No client field can provide a command,
script, URL, port, profile, or path.

## API

```text
GET  /health
POST /runs/start
GET  /runs/{run_id}
GET  /runs/{run_id}/logs?after=0
POST /runs/{run_id}/stop
POST /runs/{run_id}/register
POST /runs/{run_id}/import
```

Start body:

```json
{
  "channel": "xhs",
  "boot": true,
  "limit": 20,
  "restart": false,
  "smoke": false,
  "kill_chrome": false
}
```

`channel` is `xhs` (CDP 9222) or `douyin` (CDP 9224). Unknown fields return
`403 UNSUPPORTED_FIELDS`; the runner never evaluates a client-supplied command.
The scraper process is marked `running` only after a real PID is returned. Its
terminal status and exit code come from the child process. Logs are returned by
cursor, so the UI can poll without losing lines.

`register` and `import` are allowed only after the process exits. They write an
atomic local ledger at `.runner/ledger.json` (override with
`RUNNER_LEDGER_PATH`) and import the JSON file emitted by the run.

## HTTPS hosted pages

For an HTTPS-hosted page, expose the runner through HTTPS or a same-origin
reverse proxy. Set `RUNNER_ALLOWED_ORIGINS` to the exact page origin; the
default allows only local Workbench development origins. TLS certificate and
key arguments belong to the deployment command and are deliberately not
accepted in the browser request.
