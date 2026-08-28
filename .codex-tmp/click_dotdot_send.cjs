"use strict";

const http = require("node:http");

function getJson(requestPath) {
  return new Promise((resolve, reject) => {
    const request = http.get({ hostname: "127.0.0.1", port: 9222, path: requestPath, timeout: 5000, agent: false }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("9222 timeout")));
  });
}

class Cdp {
  constructor(url) {
    this.url = url;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.socket = new WebSocket(this.url);
      this.socket.onopen = resolve;
      this.socket.onerror = () => reject(new Error("CDP websocket failed"));
      this.socket.onmessage = (event) => {
        const message = JSON.parse(String(event.data));
        if (!message.id) return;
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
      };
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result?.value;
}

async function state(cdp, sessionId) {
  return evaluate(cdp, sessionId, `(() => {
    const visible = (node) => {
      if (!node) return false;
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && node.offsetParent !== null;
    };
    const composer = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')).filter(visible).at(-1);
    const prompt = composer ? (composer.value ?? composer.innerText ?? composer.textContent ?? "") : "";
    const pendingImages = Array.from(document.querySelectorAll('input[type="file"]')).reduce((n, input) => n + (input.files?.length || 0), 0);
    const previews = Array.from(document.querySelectorAll('.image-pure-upload-preview__item img')).filter(visible).length;
    return {
      roundCount: document.querySelectorAll(".round-item").length,
      answerCount: document.querySelectorAll(".ai-message.ai-message-finished .markdown-block, .ai-message.ai-message-finished").length,
      pendingPromptLength: String(prompt).trim().length,
      pendingImageCount: Math.max(pendingImages, previews),
    };
  })()`);
}

async function main() {
  const version = await getJson("/json/version");
  const cdp = new Cdp(version.webSocketDebuggerUrl);
  await cdp.connect();
  const targets = (await cdp.send("Target.getTargets")).targetInfos.filter(
    (target) => target.type === "page" && String(target.url || "").includes("xiaohongshu.com/ai_chat"),
  );
  if (targets.length !== 1) throw new Error(`expected one chat target, found ${targets.length}`);
  const attached = await cdp.send("Target.attachToTarget", { targetId: targets[0].targetId, flatten: true });
  const sessionId = attached.sessionId;
  await cdp.send("Runtime.enable", {}, sessionId);
  const before = await state(cdp, sessionId);
  if (before.pendingImageCount !== 1 || before.pendingPromptLength < 100) {
    throw new Error(`pending input mismatch: ${JSON.stringify(before)}`);
  }
  const clicked = await evaluate(cdp, sessionId, `(() => {
    const visible = (node) => {
      if (!node) return false;
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && node.offsetParent !== null;
    };
    const selectors = [
      ".textarea-container .bottom-box-right-submit-button .submit-button-wrapper",
      ".textarea-container .bottom-box-right-submit-button",
      ".input-container .bottom-box-right-submit-button .submit-button-wrapper",
      ".input-container .bottom-box-right-submit-button",
      ".input-container .submit-button-wrapper",
      ".wendian-btn",
    ];
    for (const selector of selectors) {
      const node = Array.from(document.querySelectorAll(selector)).filter(visible).at(-1);
      if (!node) continue;
      const disabled = node.matches("button") ? node.disabled : node.closest("button")?.disabled;
      if (disabled) continue;
      node.click();
      return { selector, tag: node.tagName, className: node.className };
    }
    return null;
  })()`);
  if (!clicked) throw new Error("send control not found");
  await new Promise((resolve) => setTimeout(resolve, 2500));
  const after = await state(cdp, sessionId);
  process.stdout.write(`${JSON.stringify({ clicked, before, after }, null, 2)}\n`);
  cdp.socket.close();
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
