# 飞书机器人接入指南(messaging/platforms/feishu.py)

本仓库的消息平台层新增了飞书(Lark)适配器:WebSocket 长连接模式,**不需要公网 IP
和回调 URL**,适合 WSL/本机部署。消息由既有的 `ClaudeMessageHandler` + CLI 会话链路
回复(`AGENT_BACKEND` 决定 claude/codex)。

## 一、飞书开放平台侧(一次性,约 10 分钟)

1. 打开 [开放平台](https://open.feishu.cn) → 创建「企业自建应用」,记下
   **App ID**(`cli_` 开头)和 **App Secret**。
2. 应用能力 → 添加「机器人」。
3. 权限管理 → 开通:`im:message`(获取与发送单聊、群组消息)、
   `im:message:send_as_bot`(以应用身份发消息)。
4. 事件与回调 → 订阅方式选 **「长连接」** → 事件订阅添加
   `im.message.receive_v1`(接收消息)。
5. 版本管理与发布 → 创建版本 → 发布(企业自建应用一般管理员即审即过)。

## 二、本仓库配置

`.env`(.env.example 已有模板):

```bash
MESSAGING_PLATFORM="feishu"
FEISHU_APP_ID="cli_xxxxxxxx"
FEISHU_APP_SECRET="xxxx"
# 可选白名单(逗号分隔);留空 = 不限制
ALLOWED_FEISHU_OPEN_IDS=""     # 用户 open_id(ou_ 开头)
ALLOWED_FEISHU_CHAT_IDS=""     # 会话 chat_id(oc_ 开头)
FEISHU_REQUIRE_MENTION=1       # 群聊必须 @机器人才响应;单聊始终响应
FEISHU_BOT_OPEN_ID=""          # 可选,配了之后群聊 @ 判定更精确
```

然后正常启动 API 服务即可(`messaging_platform` 由 `api/app.py` 装配)。

## 三、行为说明

- **单聊**:直接发消息即可(白名单若配置了 open_id,则仅白名单用户可用)。
- **群聊**:默认必须 @机器人;`requireMention` 由 `FEISHU_REQUIRE_MENTION` 控制;
  未配 `FEISHU_BOT_OPEN_ID` 时,任何 @ 都视为 @机器人(建议配上以避免误触发)。
- **支持的消息类型**:text、post(富文本,按块拼成纯文本);图片/文件/语音等
  类型目前忽略并记日志。
- **消息不可编辑**:飞书纯文本消息不支持编辑 API,进度类消息不会原地刷新,
  最终回复以新消息送达(`edit_message` 是有意的 no-op)。
- **重复投递**:按 `event_id` 去重(飞书事件可能重推)。
- **限流**:复用全局 `MessagingRateLimiter`(`MESSAGING_RATE_LIMIT/WINDOW`)。

## 四、验收清单

- [ ] 私聊机器人发"你好" → 收到回复
- [ ] 群里不 @ 机器人 → 不响应;@机器人 + 问题 → 响应
- [ ] 非白名单用户私聊 → 无响应(配置了白名单时)
- [ ] 服务重启后机器人自动重连(WS 自动重连由 SDK 处理)

## 五、排查

- 完全无响应:先确认应用已发布、机器人能力已添加、事件订阅是「长连接」且
  已订阅 `im.message.receive_v1`。
- 收到消息但不回复:查日志里 `Feishu chat/user ... not allowlisted` 或
  `message type not supported`。
- 401/99991xxx:App Secret 错误或权限未开通/未发布。
