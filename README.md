# 微信 iLink Bot 接入 ChatGPT 与 Codex

这是一个本地微信 Bot 桥接器：通过微信官方 iLink HTTP/JSON API 接收消息，普通消息交给个人 ChatGPT Plus 网页会话，带“干活”前缀的消息交给本机 Codex 处理预配置项目。

项目不再读取或操作 PC 微信窗口，不需要安装或保持 PC 微信在线，也不使用 UI Automation、Hook、微信数据库或第三方微信 CLI。

## 工作方式

- 通过微信扫码授权 Bot，凭证只保存在本机 `.runtime`。
- 使用 `getupdates` 长轮询接收消息，游标和待处理消息会持久化。
- 回复使用入站消息的 `from_user_id` 和 `context_token`，不会按昵称猜测联系人。
- 每个 iLink 用户有独立的 ChatGPT 网页会话。
- 同一个 `.runtime` 同时只允许运行一个会使用登录资料或消息状态的程序实例。
- ChatGPT/Codex 异步任务和待发送结果会先写入本地状态，再确认或投递对应消息。
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

直接运行 `start.ps1`（也可双击 `start.cmd`），或在 VS Code 选择“启动微信助手（自动检查登录）”。启动器会按顺序：

1. 在可见终端中检查微信 iLink 凭证；没有凭证时显示二维码。
2. 检查 ChatGPT Plus 登录状态；未登录时自动打开登录浏览器。
3. 两项登录都完成后启动微信助手。

```powershell
.\start.ps1
```

启动脚本先通过控制台程序完成登录，再用 `--skip-login-check` 启动桌面界面，因此首次扫码或浏览器登录不会藏在 GUI 子系统的不可见终端中。若直接运行 GUI 且缺少登录资料，界面会提示改用 `start.ps1` 或 `start.cmd`。

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

Codex 的新任务和续作都固定使用 `workspace-write` 沙箱；续作前还会重新确认历史项目别名与路径仍在当前 `projects` 配置中，项目被移除、改址或不再是 Git 仓库时会拒绝执行。

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

后台日志位于 `.runtime/bridge.log`，单个文件最多 5 MB，并保留 3 个历史文件；启动器自身错误仍写入 `.runtime/bridge.err.log`。

除只做环境检查的 `doctor` 外，命令行入口和 GUI 都会在访问登录资料或消息状态前持有 `.runtime/wechat-codex.lock` 的操作系统文件锁；同一运行目录的第二个实例会直接报错。锁文件本身可能在正常退出或崩溃后继续存在，是否正在运行以操作系统锁为准，不需要手工删除该文件。

前台命令行用 `Ctrl+C` 停止并清理浏览器和子进程；GUI 的停止和退出清理在后台线程执行，避免界面卡死。界面只停止桥接但没有退出时仍会持有单实例锁。`stop.ps1` 只用于 `start-background.ps1` 启动的后台实例：它先请求优雅关闭并等待最多 30 秒，超时后才强制结束完整进程树。微信中的 `@停止` 只停止当前 ChatGPT/Codex 任务，不会关闭桥接服务。

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

控制命令必须以 `@` 开头并与整条消息完全匹配。任何以 `@` 开头但未匹配的消息都会直接返回“命令不存在”，不会发送给 ChatGPT 或 Codex，也不会发送“正在处理”确认。不带 `@` 的“状态”“重试”等文字仍会作为普通聊天发送给 ChatGPT。每条中文命令也支持拼音首字母缩写，且缩写不区分大小写，例如 `@重试`、`@cs` 和 `@CS` 等效。

- `@帮助`（`@bz`，或 `@发送帮助` / `@fsbz`）：列出所有命令、格式和描述。
- `@新对话`（`@xdh`，或 `@清理上下文` / `@qlsxw`）：下一条消息创建新对话，旧对话保留。
- `@当前对话`（`@dqdh`）：查看当前对话标题和 ChatGPT 地址。
- `@对话列表`（`@dhlb`）、`@切换对话：2`（`@qhdh：2`）：列出并恢复当前微信用户的历史对话。
- `@归档对话`（`@gddh`）、`@重命名对话：标题`（`@cmmdh：标题`）：归档或重命名当前对话。
- `@导出对话`（`@dcdh`）：抓取当前对话并生成 Markdown，以微信文件附件发送。
- `@总结对话`（`@zjdh`）：让 ChatGPT 总结完整上下文，成功后开启新对话。
- `@重试`（`@cs`）：重新处理上一条问题；图片任务会要求重新生成。
- `@重发`（`@cf`）：不请求 ChatGPT，重发最后一次成功回复。
- `@干活：任务`（`@gh：任务`）、`@干活 项目名：任务`（`@gh 项目名：任务`）：让 Codex 在当前或指定项目执行。
- `@继续：要求`（`@jx：要求`）、`@最近任务`（`@zjrw`）：继续 Codex 会话或查看最近任务及结果。
- `@项目列表`（`@xmlb`）、`@切换项目：项目名`（`@qhxm：项目名`）：查看或切换当前微信用户的默认 Codex 项目。
- `@状态`（`@zt`）、`@停止`（`@tz`）：查看或停止当前任务。
- `@缓存状态`（`@hczt`）、`@清理缓存：7天`（`@qlhc：7天`）：查看或清理图片缓存。
- `@查看日志：50`（`@ckrz：50`）：返回最近 50 条脱敏日志，范围为 1 到 200 条。
- `@健康检查`（`@jkjc`）：查看微信连接、ChatGPT 浏览器、Codex 和缓存状态。
- `@重连微信`（`@clwx`）、`@重启浏览器`（`@cqllq`）：重新建立微信连接或重启隐藏浏览器。

Codex、项目、日志、缓存清理和故障恢复命令仅允许 `ilink_codex_user_ids` 中的用户；该列表为空时仅允许扫码授权者。

服务启动时只创建轻量浏览器工作线程，不启动 Chromium；首个 ChatGPT 聊天或对话管理请求到来时才按需打开隐藏浏览器，Codex-only 请求不会触发它。浏览器会话连续空闲 30 分钟后会自动释放，下一次请求再重新打开。此时 `@健康检查` 显示“ChatGPT 浏览器：未运行”是正常状态，可结合下一行的 ChatGPT 状态判断。若导航阶段出现 socket、代理连接或页面上下文失效，程序会重建浏览器并继续原请求，最多自动恢复 3 次，无需微信用户重新发送；旧浏览器未能在超时内关闭时，`@重启浏览器` 会明确返回失败，不会报告虚假的重启成功。

## 本地状态

`.runtime` 中的关键文件：

- `ilink-account.json`：登录凭证，敏感。
- `ilink-state.json`：长轮询游标、最新会话上下文、待处理和已处理消息 ID。
- `runtime_account.json`：将消息、任务和出站队列绑定到当前 iLink Bot，阻止换号后误用旧状态。
- `chatgpt-plus-profile/`：ChatGPT Plus 浏览器登录资料。
- `chatgpt_web_conversations.json`：iLink 用户到 ChatGPT 对话 URL 的映射。
- `chatgpt_conversation_titles.json`：用户手动设置的 ChatGPT 对话标题。
- `chatgpt_conversation_history.json`：按微信用户隔离的最近对话索引。
- `pending_chats.json`：等待串行处理的 ChatGPT 请求及其原始回复目标。
- `active_jobs.json`：已接管但尚未持久化完成结果的 ChatGPT/Codex 异步任务。
- `outbound_queue.json`：尚未成功投递到微信的异步回复及重试状态；发送完成后移除。
- `outbound_dead_letters.json`：永久失败或重试耗尽的异步回复，保留最近 200 条。
- `codex_task_history.json`：最近 Codex 任务、状态和结果。
- `selected_projects.json`：各微信用户选择的默认 Codex 项目。
- `wechat-codex.lock`：单实例操作系统锁的载体；文件存在不等于实例仍在运行。
- `chatgpt-exports/`：导出的 Markdown 对话文件。
- `inbound-images/`、`chatgpt-images/`：微信入站图片和 ChatGPT 回复图片缓存。

收到消息后会先以临时文件加原子替换的方式保存待处理列表和下一游标，保存成功后才更新内存状态；业务路由完成后才标记为已处理。连续保存失败会让轮询连接停止并重建，不会在未落盘时继续推进游标。iLink 待处理消息达到 1000 条时会暂停继续拉取；已处理消息 ID 和最近回复上下文各保留最多 2000 条。无法解析为受支持文本、语音或图片的入站私聊会记录在 `ilink-state.json` 的 `inbound_dead_letters` 中，保留最近 200 条。

ChatGPT 忙碌时，后续聊天请求会持久化排队，最多 100 条；达到上限会立即提示稍后再发。异步任务在启动前写入 `active_jobs.json` 并固定原始 `user_id`/`context_token`，完成结果先写入 `outbound_queue.json`，再清除活动任务记录，避免另一位用户的新消息覆盖回复目标。损坏或格式错误的关键状态文件会使程序停止启动，以免用空状态覆盖原数据。

出站队列使用稳定的 `client_id` 重放文本分片、图片和文件，并逐条发送；某一条失败不会阻塞后面的可发送项目。临时失败按 1、2、4 秒等指数退避，最长间隔 300 秒，累计 20 次失败后转入死信；附件不存在、超过 20 MB、内容为空或缺少文件名等永久错误会直接进入死信。异步出站队列本身不按条数丢弃，项目会保留到发送成功或进入死信。稳定 ID 能降低崩溃重放造成的重复投递，但整个链路不承诺 exactly-once。

GUI 手工发送的消息也会先进入同一持久化出站队列；桥接处于连接、重连或停止状态时界面不会接受发送，避免关闭过程中只留下一条内存消息。

若进程在异步任务执行期间硬崩溃，外部 ChatGPT/Codex 动作可能已经完成，也可能没有完成，程序无法可靠判定。重启后它不会自动重跑该任务，而会向原始回复目标发送“完成状态未知，请先确认当前状态，再决定是否重试”的提示。正常停止会尽量收集并持久化最终结果，但操作系统强杀、断电或磁盘故障仍可能落入上述状态未知路径。

## 故障排查

命令行 `doctor` 会检查 Python、Codex 命令、使用临时资料目录启动自动化浏览器、已保存的 ChatGPT Plus 登录资料、iLink 凭证和所有配置项目；加 `--connect` 后，仅在前述检查都通过时再测试 iLink 长轮询。普通 `doctor` 不获取单实例锁；`doctor --connect` 会获取运行锁，避免与正在运行的桥接同时访问 iLink 状态。

微信 `@健康检查` 会返回 iLink 连接状态、浏览器进程状态、ChatGPT/Codex 任务状态、待发送/出站死信/入站死信数量、最近一条出站死信的时间与脱敏错误，以及图片缓存大小。`@状态` 还会显示当前 ChatGPT 排队条数；需要更多上下文时可用 `@查看日志：50` 查看已脱敏日志。

- 提示“尚未登录微信 iLink”：运行 `ilink-login`。
- 返回 `-14` 或提示凭证失效：运行 `ilink-login --force`。
- 二维码要求数字验证：输入手机微信显示的数字。
- ChatGPT 登录失效：重新运行 `chatgpt-login`。
- 提示已有实例运行：先退出 GUI、用 `Ctrl+C` 结束前台实例，或对后台实例执行 `stop.ps1`；不要通过删除锁文件绕过检查。
- `@健康检查` 显示浏览器“未运行”但 ChatGPT 状态为空闲：通常只是超过 30 分钟后按设计释放，下一次 ChatGPT 请求会重新打开。
- 出现出站死信：先根据最近死信错误修复网络或附件问题；死信只保留诊断记录，不会自动重新入队。
- 代理只影响 ChatGPT 浏览器；iLink API 使用系统网络和正常 TLS 校验。
- 修改依赖前先执行 `stop.ps1`，避免正在运行的入口程序锁定 `.venv\Scripts`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

iLink 协议实现对齐腾讯官方 `@tencent-weixin/openclaw-weixin` 2.4.6 的 HTTP 请求格式；协议升级时应先更新契约测试，再调整版本头和请求体。
