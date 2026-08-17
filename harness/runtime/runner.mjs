import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let entry;
try {
  // rc.6 exposes the generic launcher through the package export map; the
  // internal lib/ path is intentionally not importable.
  entry = require.resolve("@deepseek-ai/dsh-sdk-jsonrpc-demo/bin");
} catch (error) {
  console.error(
    "DeepSeek Harness runtime dependencies are missing. Run npm install in harness/runtime.",
  );
  process.exitCode = 1;
}

if (entry) {
  const child = spawn(process.execPath, [entry, ...process.argv.slice(2)], {
    env: process.env,
    stdio: "inherit",
  });
  child.once("error", (error) => {
    console.error(`DeepSeek Harness runtime failed to start: ${error.message}`);
    process.exitCode = 1;
  });
  child.once("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
    } else {
      process.exitCode = code ?? 1;
    }
  });
}
