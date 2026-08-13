# 企业微信 ChatGpt 群机器人

本程序通过企业微信“智能机器人长连接 API”监听群消息，再把普通问题交给个人 ChatGPT Plus 网页，把明确的开发命令交给本机 Codex。企业微信端完全使用 API，不控制企业微信窗口，也不需要公网回调地址。

```text
企业微信群 ChatGpt 中 @机器人
        ↓ WebSocket aibot_msg_callback
本程序：群限制、成员权限、去重、命令路由
        ├─ 普通问题 → 后台 Chrome → ChatGPT Plus
        └─ 干活命令 → 本机 codex exec
        ↓ WebSocket aibot_respond_msg
企业微信群中的机器人回复
```

## 1. 创建并加入群

1. 在企业微信中创建“智能机器人”，启用 **API 模式**。
2. 接入方式选择 **长连接**，复制 `BotID` 和长连接 `Secret`。
3. 把机器人加入企业微信群 **ChatGpt**。
4. 把 `BotID` 写到 [config.yaml](config.yaml) 的 `wecom_bot_id`。

企业微信的长连接地址固定为 `wss://openws.work.weixin.qq.com`。一个机器人同时只能保留一条有效长连接；重复启动程序或运行连接检查会让旧连接断开并自动重连。

官方文档：[智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463)

## 2. 配置密钥

只有企业微信机器人的 Secret 需要环境变量。PowerShell 当前窗口临时设置：

```powershell
$env:WECOM_BOT_SECRET = "企业微信机器人的长连接 Secret"
```

需要持久保存到当前 Windows 用户时：

```powershell
[Environment]::SetEnvironmentVariable("WECOM_BOT_SECRET", "你的 Secret", "User")
```

持久设置后重新打开 VS Code。不要把 Secret 写进 YAML、提交到 Git，或贴到聊天记录中。

普通聊天复用你的 ChatGPT Plus 网页账号，**不需要 `OPENAI_API_KEY`，也不调用 OpenAI Platform API**。它属于个人网页自动化，不是 ChatGPT 官方 API；页面结构变化、登录验证和 Plus 用量限制仍可能影响运行。

## 3. 安装与检查

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wechat-codex.exe --config config.yaml chatgpt-login
.\.venv\Scripts\wechat-codex.exe --config config.yaml doctor
```

`chatgpt-login` 会打开一个自动化专用的 Chrome。请在该窗口登录 ChatGPT Plus，看到输入框后回到终端按 Enter。日常运行使用这个专用登录资料，不会占用你当前已打开的普通 Chrome。

需要实际验证企业微信订阅时，先停止正在运行的机器人，再执行：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml doctor --connect
```

## 4. 在 ChatGpt 群测试

启动程序：

```powershell
.\start.ps1
```

或者在 VS Code 的“运行和调试”中选择 **启动企业微信 ChatGpt 群机器人**。

然后在企业微信群 **ChatGpt** 中发送：

```text
@机器人 你好，请回复“测试成功”
```

程序收到消息时会打印类似日志：

```text
收到企业微信消息：chat_type=group chatid=wrxxxxxxxx userid=xxxx msgid=xxxx
```

企业微信回调只给 `chatid`，不给群名称。首次确认该日志确实来自“ChatGpt”群后，把 ID 写回配置，防止机器人在其他群响应：

```yaml
group_only: true
allowed_group_chat_ids:
  - "wrxxxxxxxx"
```

测试阶段 `allowed_group_chat_ids: []` 表示响应机器人所在的所有群；最安全的做法是测试时只把机器人加入 ChatGpt 群，取得 ID 后立即锁定。

## 5. 消息与权限

普通文字直接进入 ChatGPT Plus 网页对话。群聊会话按 `群 chatid + 发言者 userid` 隔离，同一群不同成员不会共享上下文。

ChatGPT 侧边栏的对话标题由企业微信会话名称决定。机器人回调不包含可读名称，因此需要在配置中维护映射：

```yaml
# 当前测试群没有单独映射时使用该名称
default_group_chat_name: "ChatGpt"

group_chat_names:
  "wrxxxxxxxx": "ChatGpt"

user_chat_names:
  "zhangsan": "张三"
```

群聊优先使用 `chatid → 群名`，没有映射时使用 `default_group_chat_name`；个人聊天使用 `userid → 姓名`，没有映射时暂用 `userid`。若要同时接收机器人单聊，将 `group_only` 改为 `false`。新旧 ChatGPT 对话都会在下一次收到消息时校准标题。

以下命令会进行特殊路由：

- `帮助`：显示命令
- `新对话`：切换该成员在该群的 ChatGPT 网页对话
- `干活：任务内容`：让本机 Codex 修改默认项目
- `干活 项目名：任务内容`：修改指定项目
- `继续：补充要求`：继续最近一次 Codex 任务
- `状态`：查看 ChatGPT Plus/Codex 状态
- `停止`：停止当前请求或任务

涉及本机 Codex 的命令只允许 [config.yaml](config.yaml) 中 `authorized_senders` 列出的企业微信 `userid`。第一次群测试后可从日志复制实际 `userid`，再写入白名单。

## 6. 后台运行

```powershell
.\start-background.ps1
.\stop.ps1
```

日志位于 `.runtime/bridge.out.log` 和 `.runtime/bridge.err.log`。企业微信始终通过后台 API 收发，不会打开或激活企业微信窗口。`chatgpt_headless: true` 时，日常 ChatGPT 聊天也不显示 Chrome；只有首次登录、登录过期或出现人机验证时才需要可见浏览器。
