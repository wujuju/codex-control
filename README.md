# 微信 iLink Bot 接入 ChatGPT 与 Codex

这是一个本地微信 Bot 桥接器：通过微信官方 iLink HTTP/JSON API 接收消息，普通消息交给个人 ChatGPT Plus 网页会话，带“干活”前缀的消息交给本机 Codex 处理预配置项目。

项目不再读取或操作 PC 微信窗口，不需要安装或保持 PC 微信在线，也不使用 UI Automation、Hook、微信数据库或第三方微信 CLI。

## 工作方式

- 通过微信扫码授权 Bot，凭证只保存在本机 `.runtime`。
- 使用 `getupdates` 长轮询接收消息，游标和待处理消息会持久化。
- 回复使用入站消息的 `from_user_id` 和 `context_token`，不会按昵称猜测联系人。
- 每个 iLink 用户有独立的 ChatGPT 网页会话。
- 默认只接受扫码授权者的消息；Codex 命令有单独的用户 ID 白名单。
- 支持文本，以及 iLink 消息中自带文字转写的语音消息。

当前按官方 iLink Bot 的私聊语义工作，不支持原 PC 微信方案中的群聊 `@机器人` 监听。

## 环境

- Windows 10/11
- Python 3.10–3.13
- Codex CLI
- Edge 或 Chrome
- 可以访问 `https://ilinkai.weixin.qq.com` 和 `https://chatgpt.com`

## 安装

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

复制并修改配置：

```powershell
Copy-Item config.example.yaml config.yaml
```

`projects` 中只能登记允许 Codex 操作的 Git 仓库。微信消息不能提供任意磁盘路径。

## 登录与启动

直接运行 `start.ps1`，或在 VS Code 选择“启动微信助手（自动检查登录）”。启动器会按顺序：

1. 检查微信 iLink 凭证；没有凭证时在终端显示二维码。
2. 检查 ChatGPT Plus 登录状态；未登录时自动打开登录浏览器。
3. 两项登录都完成后启动微信助手。

```powershell
.\start.ps1
```

扫码并在手机微信确认后，以下内容写入 `.runtime/ilink-account.json`：

- `bot_token`
- Bot 账号 ID
- 扫码用户的稳定 iLink 用户 ID
- 服务端返回的业务 API 地址

凭证文件已被 `.gitignore` 排除，不要复制到仓库或聊天中。如需主动重新授权，可单独运行：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml ilink-login --force
```

## 配置授权用户

默认配置中的两个列表为空：

```yaml
ilink_allowed_user_ids: []
ilink_codex_user_ids: []
```

空列表表示只允许首次扫码授权者。需要增加用户时，填写日志中看到的完整 `from_user_id`：

```yaml
ilink_allowed_user_ids:
  - "user-id-a@im.wechat"
  - "user-id-b@im.wechat"

ilink_codex_user_ids:
  - "user-id-a@im.wechat"
```

`ilink_allowed_user_ids` 控制普通聊天；`ilink_codex_user_ids` 控制“干活、继续、状态、停止”。不要把不可信用户加入 Codex 白名单。

ChatGPT 网页对话使用固定标题，可直接修改：

```yaml
chatgpt_conversation_title: "微信助手"
```

标题会在下一次成功回复后同步到对应的 ChatGPT 对话。微信图片会从官方 CDN 下载、解密并上传到 ChatGPT，缓存位于 `.runtime/inbound-images/`；ChatGPT 回复中的图片也会被保存、加密上传到微信 CDN，再作为原生图片消息发回微信，缓存位于 `.runtime/chatgpt-images/`。单张图片限制为 20 MB。语音消息使用 iLink 提供的 `voice_item.text` 转写原文；没有转写文本的音频不会发送给 ChatGPT。

## 启动

先检查环境：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml doctor --connect
```

命令行运行：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml start
```

桌面界面（包含自动登录检查）：

```powershell
.\start.ps1
```

后台运行也会先在当前终端完成登录检查，再转入隐藏后台：

```powershell
.\start-background.ps1
.\stop.ps1
```

后台日志位于 `.runtime/bridge.err.log`。

## 辅助命令

只读取 iLink 消息，不调用 ChatGPT/Codex：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml read
```

向扫码用户发送测试消息：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml send "连接测试成功"
```

向指定用户 ID 发送：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml send "测试" --to "user-id@im.wechat"
```

没有入站 `context_token` 时，主动消息由 iLink 服务端决定是否允许投递；正常回复始终使用对应入站上下文。

## 微信命令

- 普通文本：交给 ChatGPT Plus。
- `新对话`：下一条普通问题使用新的 ChatGPT 对话。
- `干活：任务描述`：让 Codex 在默认项目执行。
- `干活 项目别名：任务描述`：在指定项目执行。
- `继续：补充要求`：继续最近一次 Codex 会话。
- `状态`：查看当前 ChatGPT/Codex 状态。
- `停止`：停止当前任务。
- `帮助`：显示命令和项目别名。

## 本地状态

`.runtime` 中的关键文件：

- `ilink-account.json`：登录凭证，敏感。
- `ilink-state.json`：长轮询游标、最新会话上下文、待处理和已处理消息 ID。
- `chatgpt-plus-profile/`：ChatGPT Plus 浏览器登录资料。
- `chatgpt_web_conversations.json`：iLink 用户到 ChatGPT 对话 URL 的映射。

收到消息后会先落盘到待处理列表，再推进长轮询游标；业务路由完成后才标记为已处理，以减少重启导致的遗漏或重复执行。

## 故障排查

- 提示“尚未登录微信 iLink”：运行 `ilink-login`。
- 返回 `-14` 或提示凭证失效：运行 `ilink-login --force`。
- 二维码要求数字验证：输入手机微信显示的数字。
- ChatGPT 登录失效：重新运行 `chatgpt-login`。
- 代理只影响 ChatGPT 浏览器；iLink API 使用系统网络和正常 TLS 校验。
- 修改依赖前先执行 `stop.ps1`，避免正在运行的入口程序锁定 `.venv\Scripts`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

iLink 协议实现对齐腾讯官方 `@tencent-weixin/openclaw-weixin` 2.4.6 的 HTTP 请求格式；协议升级时应先更新契约测试，再调整版本头和请求体。
