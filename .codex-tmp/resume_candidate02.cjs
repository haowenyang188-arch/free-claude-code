"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const batchDir = "E:\\AI\\xhs-evidence\\xhs-search-20260828-002\\05_dotdot";
const manifestPath = path.win32.join(batchDir, "dotdot-manifest.json");
const eventsPath = path.win32.join(batchDir, "events.jsonl");
const replyPath = path.win32.join(batchDir, "replies", "candidate02-01.txt");
const screenshotPath = path.win32.join(batchDir, "screenshots", "candidate02-01.png");
const maxWaitMs = 180000;
const pollMs = 2000;

function nowIso() { return new Date().toISOString(); }
function sha256File(filePath) { return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex"); }
function appendEvent(stage, status, detail = {}) {
  fs.appendFileSync(eventsPath, `${JSON.stringify({ event_id: crypto.randomUUID(), occurred_at: nowIso(), stage, status, ...detail })}\n`, "utf8");
}
function writeAtomic(filePath, contents) {
  fs.mkdirSync(path.win32.dirname(filePath), { recursive: true });
  const temp = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(temp, contents, { encoding: "utf8", flag: "wx" });
  try { fs.renameSync(temp, filePath); } finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
}
function saveManifest(manifest) {
  manifest.updated_at = nowIso();
  const temp = `${manifestPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(temp, `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  try { fs.renameSync(temp, manifestPath); } finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
}
function getJson(requestPath) {
  return new Promise((resolve, reject) => {
    const req = http.get({ hostname: "127.0.0.1", port: 9222, path: requestPath, timeout: 5000, agent: false }, (res) => {
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
  constructor(url) { this.url = url; this.socket = null; this.nextId = 1; this.pending = new Map(); }
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
        if (message.error) pending.reject(new Error(message.error.message)); else pending.resolve(message.result || {});
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
  const result = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluate failed");
  return result.result?.value;
}
async function pageState(cdp, sessionId) {
  return evaluate(cdp, sessionId, `(() => {
    const visible = (node) => {
      if (!node) return false;
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden" && node.offsetParent !== null;
    };
    const answers = Array.from(document.querySelectorAll(".ai-message.ai-message-finished .markdown-block, .ai-message.ai-message-finished"))
      .map((node) => (node.innerText || node.textContent || "").trim()).filter(Boolean);
    const body = document.body?.innerText || "";
    return {
      url: location.href,
      roundCount: document.querySelectorAll(".round-item").length,
      answerCount: answers.length,
      latestAnswer: answers.at(-1) || "",
      pendingPrompt: (() => { const nodes = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')).filter(visible); const node = nodes.at(-1); return node ? String(node.value ?? node.innerText ?? node.textContent ?? "").trim() : ""; })(),
      pendingImageCount: Math.max(Array.from(document.querySelectorAll('input[type="file"]')).reduce((n, input) => n + (input.files?.length || 0), 0), Array.from(document.querySelectorAll('.image-pure-upload-preview__item img')).filter(visible).length),
      bodyTail: body.slice(-3000),
      loginRequired: /扫码登录|登录后查看|验证码/.test(body),
      unavailable: /当前笔记暂时无法浏览|内容不存在|页面不存在/.test(body),
      generating: /停止生成|正在生成|生成中|回答中|思考中/.test(body),
    };
  })()`);
}
async function captureScreenshot(cdp, sessionId) {
  const result = await cdp.send("Page.captureScreenshot", { format: "png", fromSurface: true }, sessionId);
  if (fs.existsSync(screenshotPath)) throw new Error(`截图已存在，拒绝覆盖：${screenshotPath}`);
  fs.mkdirSync(path.win32.dirname(screenshotPath), { recursive: true });
  fs.writeFileSync(screenshotPath, Buffer.from(result.data, "base64"), { flag: "wx" });
}
async function main() {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const job = manifest.jobs.find((item) => item.job_id === "candidate02-01");
  if (!job) throw new Error("candidate02-01 not in manifest");
  if (job.state !== "failed") throw new Error(`candidate02-01 state is ${job.state}, refusing resume`);
  if (job.reply_sha256 || job.screenshot_sha256 || fs.existsSync(replyPath) || fs.existsSync(screenshotPath)) throw new Error("candidate02 archive already exists; refusing overwrite");
  const version = await getJson("/json/version");
  const targets = (await getJson("/json/list")).filter((target) => target.type === "page" && String(target.url || "").includes("xiaohongshu.com/ai_chat"));
  if (targets.length !== 1) throw new Error(`expected one chat target, found ${targets.length}`);
  const cdp = new Cdp(version.webSocketDebuggerUrl);
  await cdp.connect();
  const attached = await cdp.send("Target.attachToTarget", { targetId: targets[0].id, flatten: true });
  const sessionId = attached.sessionId;
  await cdp.send("Runtime.enable", {}, sessionId);
  await cdp.send("Page.enable", {}, sessionId);
  const baseline = { roundCount: 1, answerCount: 2 };
  const initial = await pageState(cdp, sessionId);
  if (initial.pendingImageCount || initial.pendingPrompt) throw new Error(`candidate02 页面仍有待发送输入：${JSON.stringify(initial)}`);
  if (initial.roundCount < baseline.roundCount || initial.answerCount < baseline.answerCount) throw new Error(`candidate02 页面轮次回退：${JSON.stringify(initial)}`);
  let lastAnswer = "";
  let stableTicks = 0;
  let found = null;
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    const state = await pageState(cdp, sessionId);
    if (state.loginRequired) throw new Error("点点页面需要用户登录或验证码");
    if (state.unavailable) throw new Error("点点页面当前不可浏览");
    if (state.roundCount > baseline.roundCount && state.answerCount > baseline.answerCount && state.latestAnswer) {
      if (state.latestAnswer === lastAnswer && !state.generating) stableTicks += 1; else stableTicks = 0;
      lastAnswer = state.latestAnswer;
      if (stableTicks >= 2) { found = state; break; }
    }
  }
  if (!found) throw new Error("candidate02 回复仍未稳定");
  job.state = "replying";
  job.failure_reason = null;
  saveManifest(manifest);
  appendEvent("step-7-dotdot", "replying", { job_id: job.job_id, result_round_number: found.roundCount, resumed: true });
  writeAtomic(replyPath, `${found.latestAnswer}\n`);
  await captureScreenshot(cdp, sessionId);
  job.reply_sha256 = sha256File(replyPath);
  job.screenshot_sha256 = sha256File(screenshotPath);
  job.state = "archived";
  saveManifest(manifest);
  job.state = "verified";
  job.verified_at = nowIso();
  saveManifest(manifest);
  appendEvent("step-7-dotdot", "verified", { job_id: job.job_id, reply_sha256: job.reply_sha256, screenshot_sha256: job.screenshot_sha256, resumed: true });
  process.stdout.write(`${JSON.stringify({ job_id: job.job_id, state: job.state, result_round_number: found.roundCount, answer_length: found.latestAnswer.length, reply_sha256: job.reply_sha256, screenshot_sha256: job.screenshot_sha256 }, null, 2)}\n`);
  cdp.socket.close();
}
main().catch((error) => { process.stderr.write(`${error.stack || error.message}\n`); process.exitCode = 1; });
