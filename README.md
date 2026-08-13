# PC 微信控制 Codex

一个纯本地 Windows 小工具：读取已登录的 PC 微信消息，普通消息交给 Codex 做只读聊天，只有带“干活”前缀的消息才允许 Codex 修改预配置项目。

桌面端使用 PySide6 + QML：左侧显示私聊/群聊会话，右侧显示收到的消息、Codex 回复和手动发送区。窗口可正常最小化，最小化后微信监听与 Codex 任务继续运行。

## 当前能力

- 固定监听一个联系人、群聊或“文件传输助手”
- 私聊直接回复；群聊只有明确 `@ChatGpt机器人` 才回复
- 只有白名单中的“無惧”可以执行干活、继续、状态和停止命令
- 普通中文聊天
- `干活：任务` 修改默认项目
- `干活 control：任务` 修改指定项目
- `继续：要求` 继续最近一次干活会话
- `状态` 查看任务
- `停止` 终止任务
- 启动时忽略已有历史消息，回复自动防循环
- 可选在登录连接成功后发送一条“已上线”消息（当前默认关闭）
- 读取微信原生自动转写结果，识别内容按普通消息继续处理

## 环境

- Windows 10/11
- Python 3.10～3.13
- PC 微信 4.x，已登录
- 已安装并登录 Codex CLI

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

编辑 `config.yaml`：

```yaml
contact: "無惧"
chat_type: "friend" # 监听群聊时改为 group
bot_name: "ChatGpt机器人"
authorized_senders: ["無惧"]
background_mode: true
voice_recognition: true
voice_retry_count: 3
default_project: "control"
projects:
  control: "."
  demo: "D:\\Sam\\demo"
```

项目必须是 Git 仓库。微信消息不能直接指定磁盘路径，只能使用这里登记的项目别名。

## 运行

先打开并登录 PC 微信，然后检查：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml doctor --connect
```

发送测试消息：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml send "连接测试成功"
```

启动可视化 QML 桌面端：

```powershell
.\start.cmd
# 或
.\.venv\Scripts\wechat-codex-gui.exe --config config.yaml
```

也可以双击 `start.cmd`。窗口右上角或界面中的“最小化”按钮都可最小化；最小化不会停止桥接。关闭窗口才会结束程序。

纯命令行监听仍可使用：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml start
```

如果 PowerShell 允许执行本地脚本，也可以在后台运行：

```powershell
.\start-background.ps1
.\stop.ps1
```

后台日志在 `.runtime/bridge.err.log`。如果微信尚未登录，程序会每 5 秒自动重试，登录后无需重启。

## 微信命令

```text
你好，解释一下什么是 Git rebase
干活：检查项目并运行测试
干活 control：给 README 增加安装说明并验证
继续：再补一个测试
状态
停止
帮助
```

监听普通联系人时请设置 `allow_self_messages: false`，只处理对方发来的消息。使用“文件传输助手”时可改成 `true`；程序发出的内容带 `[Codex助手]` 前缀并会被忽略，不会自我回复。

监听群聊时，把 `contact` 改为群名并设置 `chat_type: group`。群消息必须包含 `@ChatGpt机器人`，程序会先去掉该 @ 再进行聊天或命令解析。普通聊天对群成员开放，但 `干活`、`继续`、`状态`、`停止` 只接受 `authorized_senders` 中的发送者；当前仅允许“無惧”。为保持完全后台，程序不会动态切换并扫描其他会话，因此每个运行实例监听一个已配置会话。

## 安全边界

- 普通聊天使用 `read-only` 沙箱和临时 Codex 会话。
- 干活使用 `workspace-write`，只在配置的项目里运行。
- 系统提示明确禁止 Git 提交、推送、部署及破坏性操作。
- 不使用微信 Hook、不读取微信数据库，只通过 Windows UI Automation 操作窗口。

## 后台执行说明

`background_mode: true` 时，程序绕过会激活窗口的 `ChatBox.get_msgs()` 和语音气泡右键操作；它不激活微信窗口、不移动鼠标、不发送全局模拟按键，也不使用剪贴板。文本通过 UI Automation 的 `ValuePattern` 写入，再通过只发给微信窗口的 Win32 消息提交。桥接器自身以隐藏进程运行。

微信 4.1.12 在窗口最小化或关闭到托盘后会卸载消息控件，因此微信主窗口需要保持“已打开、未最小化”。程序会把微信放到其他窗口后面，并且不会主动切到前台。

语音依赖微信“设置 → 通用 → 聊天中的语音消息自动转成文字”开关。程序只读取微信生成的转写，不点击或右键语音气泡，因此不会为了识别语音抢占焦点。识别后的内容会直接进入聊天/命令路由，所以语音说“干活：运行测试”与发送同样文字效果一致。连续读取不到转写时只写日志，不主动给联系人发送状态提示。

这类 UI 自动化依赖微信窗口结构。微信大版本更新后若连接失败，先运行 `doctor --connect` 查看错误，并升级 `wxauto4`。
