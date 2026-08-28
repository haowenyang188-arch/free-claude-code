"use strict";

const http = require("node:http");

function getJson(path) {
  return new Promise((resolve, reject) => {
    const req = http.get({ hostname: "127.0.0.1", port: 9222, path, agent: false, timeout: 5000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("9222 timeout")));
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

  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: false,
  }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result?.value;
}

async function main() {
  const version = await getJson("/json/version");
  const tabs = await getJson("/json/list");
  const target = tabs.find((item) => item.type === "page" && String(item.url || "").includes("xiaohongshu.com/ai_chat"));
  if (!target) throw new Error("chat target not found");
  const cdp = new Cdp(version.webSocketDebuggerUrl);
  await cdp.connect();
  const attached = await cdp.send("Target.attachToTarget", { targetId: target.id, flatten: true });
  const sessionId = attached.sessionId;
  await cdp.send("Runtime.enable", {}, sessionId);
  const snapshot = await evaluate(cdp, sessionId, `(() => {
    const visible = (node) => {
      if (!node) return false;
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && node.offsetParent !== null;
    };
    const textOf = (node) => String(node?.innerText || node?.textContent || "").replace(/\\s+/g, " ").trim();
    const composer = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')).filter(visible).at(-1);
    const rounds = Array.from(document.querySelectorAll(".round-item")).map((node, index) => ({
      index,
      text: textOf(node),
      className: node.className,
      html: node.outerHTML.slice(0, 1200),
    }));
    const answers = Array.from(document.querySelectorAll(".ai-message, .markdown-block")).filter(visible).map((node, index) => ({
      index,
      className: node.className,
      text: textOf(node),
    }));
    const buttons = Array.from(document.querySelectorAll("button, [role=button], .submit-button-wrapper")).filter(visible).map((node) => ({
      text: textOf(node).slice(0, 100),
      aria: node.getAttribute("aria-label"),
      title: node.getAttribute("title"),
      className: String(node.className || ""),
      disabled: Boolean(node.disabled || node.closest("button")?.disabled),
    })).filter((item) => item.text || item.aria || item.title || item.className.includes("submit"));
    return {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      roundCount: document.querySelectorAll(".round-item").length,
      answerCount: document.querySelectorAll(".ai-message.ai-message-finished .markdown-block, .ai-message.ai-message-finished").length,
      pendingPrompt: composer ? String(composer.value ?? composer.innerText ?? composer.textContent ?? "").trim() : null,
      pendingImageCount: Array.from(document.querySelectorAll('input[type="file"]')).reduce((n, input) => n + (input.files?.length || 0), 0),
      previewCount: Array.from(document.querySelectorAll('.image-pure-upload-preview__item img')).filter(visible).length,
      rounds,
      answers,
      buttons: buttons.slice(-30),
      bodyText: textOf(document.body).slice(-6000),
    };
  })()`);
  process.stdout.write(`${JSON.stringify(snapshot, null, 2)}\n`);
  cdp.socket.close();
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
