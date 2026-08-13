# PC 微信控制 Codex

一个纯本地 Windows 小工具：读取已登录的 PC 微信消息，普通消息交给 Codex 做只读聊天，只有带“干活”前缀的消息才允许 Codex 修改预配置项目。

## 当前能力

- 固定监听一个联系人、群聊或“文件传输助手”
- 普通中文聊天
- `干活：任务` 修改默认项目
- `干活 control：任务` 修改指定项目
- `继续：要求` 继续最近一次干活会话
- `状态` 查看任务
- `停止` 终止任务
- 启动时忽略已有历史消息，回复自动防循环
- 登录连接成功后自动发送一条“已上线”消息
- 自动调用微信“语音转文字”，识别结果按普通消息继续处理

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

开始监听：

```powershell
.\.venv\Scripts\wechat-codex.exe --config config.yaml start
```

也可以双击 `start.cmd`。关闭运行窗口或按 `Ctrl+C` 停止。

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

## 安全边界

- 普通聊天使用 `read-only` 沙箱和临时 Codex 会话。
- 干活使用 `workspace-write`，只在配置的项目里运行。
- 系统提示明确禁止 Git 提交、推送、部署及破坏性操作。
- 不使用微信 Hook、不读取微信数据库，只通过 Windows UI Automation 操作窗口。

## 后台执行说明

`background_mode: true` 时，程序不激活微信窗口、不移动鼠标、不发送全局模拟按键，也不使用剪贴板；文本通过 UI Automation 的 `ValuePattern` 写入，再通过只发给微信窗口的 Win32 消息提交。桥接器自身以隐藏进程运行。

微信 4.1.12 在窗口最小化或关闭到托盘后会卸载消息控件，因此微信主窗口需要保持“已打开、未最小化”。程序会把微信放到其他窗口后面，并且不会主动切到前台。

收到语音时，程序调用微信客户端自带的语音转文字。识别后的内容会直接进入聊天/命令路由，因此语音说“干活：运行测试”与发送同样的文字效果一致。识别连续失败 `voice_retry_count` 次后会提示改发文字。

这类 UI 自动化依赖微信窗口结构。微信大版本更新后若连接失败，先运行 `doctor --connect` 查看错误，并升级 `wxauto4`。
