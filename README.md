# Codex GitHub Bridge（Issue 轮询版）

本工具把 GitHub Issue 评论当成手机控制台：

```text
手机 GitHub App / 网页评论
        ↓
GitHub Issue 评论
        ↓
本地 bridge 主动轮询 GitHub API
        ↓
调用 Codex CLI：codex exec / codex exec resume --last
        ↓
执行完成后评论回 GitHub
```

不需要 webhook，不需要公网 URL，不需要内网穿透。

> 重要：你现在使用的是 public 仓库 `wujuju/codex-control`，Issue 评论和执行结果都是公开的。不要在评论里写密钥、私有代码、账号、商业信息。强烈建议把 `GITHUB_ALLOWED_USERS` 设置成你自己的 GitHub 用户名。

---

## 1. 准备 GitHub 登录

本工具需要能读取 Issue 评论、创建 Issue、回复 Issue 评论。

### 推荐方式：复用 GitHub CLI 登录

如果你本机已经安装 GitHub CLI，并且执行过：

```bat
gh auth login
```

那么 `.env` 里的 `GITHUB_TOKEN` 可以留空。bridge 会自动尝试：

```bat
gh auth token
```

来读取当前 GitHub CLI 保存的 token。

检查方式：

```bat
gh auth status
gh auth token
```

### 备用方式：手动填 Token

如果你没有安装 GitHub CLI，才需要手动准备一个能给 `wujuju/codex-control` 发 Issue 评论的 token。

推荐 Fine-grained token：

- Repository access：选择 `wujuju/codex-control`
- Repository permissions：`Issues: Read and write`

或者 Classic PAT：

- public 仓库：`public_repo`

---

## 2. 安装

Windows CMD：

```bat
install.bat
```

PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装后会自动复制 `.env.example` 为 `.env`。

---

## 3. 配置 `.env`

最少需要改这些：

```env
GITHUB_REPO=wujuju/codex-control
# GITHUB_TOKEN 可以留空，程序会自动尝试 gh auth token
GITHUB_TOKEN=
GITHUB_ALLOWED_USERS=你的GitHub用户名
REPO_PATH=D:\Sam\HGameAI
CODEX_BIN=codex
```

如果你还没创建控制 Issue，可以保持：

```env
GITHUB_ISSUE_NUMBER=0
GITHUB_CONTROL_ISSUE_TITLE=Codex Control Console
```

启动时 bridge 会自动查找或创建标题为 `Codex Control Console` 的 Issue。

如果你已经有固定 Issue，例如 #1：

```env
GITHUB_ISSUE_NUMBER=1
```

---

## 4. 启动

CMD：

```bat
run_bridge.bat
```

PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bridge.ps1
```

启动后，手机打开 GitHub 仓库的控制 Issue，直接评论命令即可。

---

## 5. 支持命令

```text
帮助
状态
继续 <prompt>
新任务 <prompt>
总结
diff
测试
停止
```

示例：

```text
状态
```

```text
继续 检查项目是否还有旧接口残留，不要修改代码，先输出分析
```

```text
新任务 分析整个项目结构，列出最值得优先优化的 10 个点，不要修改代码
```

```text
diff
```

```text
测试
```

---

## 6. 可选：命令前缀

public 仓库里，为了避免普通评论误触发，你可以设置：

```env
COMMAND_PREFIX=!
```

这样命令要写成：

```text
!状态
!继续 检查项目是否还有旧接口残留
```

---

## 7. Codex 命令说明

默认调用：

```bat
codex exec --cd <REPO_PATH> -
```

继续任务调用：

```bat
codex exec resume --last --cd <REPO_PATH> -
```

prompt 通过 stdin 传入，所以中文和多行内容不会受命令行转义影响。

如果你的 Codex 版本对 `--cd` 参数顺序有差异，可以在 `codex_github_bridge/runner.py` 的 `_codex_args()` 里调整顺序。

---

## 8. 状态和日志

状态文件：

```text
.codex_github_bridge/state.json
```

日志目录：

```text
.codex_github_bridge/logs/
```

每次 Codex 执行都会生成一个日志文件，GitHub 评论里会显示日志路径和输出末尾。

---

## 9. 安全规则

GitHub 评论只能触发白名单命令：

- 状态
- 继续
- 新任务
- 总结
- diff
- 测试
- 停止
- 帮助

评论里不能直接执行 shell 命令。`测试` 命令执行的是你本地 `.env` 里固定配置的 `TEST_COMMAND`，不是评论里传来的命令。

因为 public 仓库任何人理论上都可能评论，所以必须设置：

```env
GITHUB_ALLOWED_USERS=你的GitHub用户名
```

如果不设置，bridge 会默认只允许 GitHub token 所属用户触发命令。

---

## 10. 常见问题

### GITHUB_TOKEN 可以不填吗？

可以，但前提是本机已经安装 GitHub CLI，并且 `gh auth status` 显示已经登录。

浏览器登录 GitHub 或 Git remote 能 push，不等于 Python 程序能直接调用 GitHub REST API。bridge 不会读取浏览器 cookie，也不会读取 Git 的 HTTPS 凭据。它只会按顺序尝试：

1. `.env` 里的 `GITHUB_TOKEN`
2. 系统环境变量 `GH_TOKEN`
3. `gh auth token`

如果三者都没有，就会报 GitHub authentication is required。



### 启动时报 401 Bad credentials

这通常是 `.env` 里 `GITHUB_TOKEN` 填了过期 token、示例 token，或系统环境变量 `GH_TOKEN` 里有旧 token。

推荐处理：

```bat
set GITHUB_TOKEN=
set GH_TOKEN=
gh auth status
gh auth login
gh auth token
```

然后把 `.env` 里的 `GITHUB_TOKEN` 留空：

```env
GITHUB_TOKEN=
```

新版会自动跳过明显的示例 token，并且启动时验证每个候选 token；如果 `GITHUB_TOKEN` 无效，会继续尝试 `GH_TOKEN` 和 `gh auth token`。

### 1）启动时报 GitHub authentication is required

没有可用认证。最简单做法：安装 GitHub CLI，然后执行 `gh auth login`。或者在 `.env` 填入有效的 `GITHUB_TOKEN`。

### 2）启动时报 REPO_PATH does not exist

把 `.env` 里的 `REPO_PATH` 改成你的真实项目路径，例如：

```env
REPO_PATH=D:\Sam\HGameAI
```

### 3）评论后没有反应

检查：

- `run_bridge.bat` 是否还在运行
- `GITHUB_ALLOWED_USERS` 是否包含你的 GitHub 用户名
- 评论是否在正确 Issue 下
- `POLL_INTERVAL_SECONDS` 是否设置太大
- GitHub token 是否有 Issues read/write 权限

### 4）Codex 命令失败

先在本地 CMD 测试：

```bat
codex --version
codex exec --cd D:\Sam\HGameAI "只输出 hello"
```

如果 `codex exec resume --last --cd ...` 参数顺序报错，编辑 `codex_github_bridge/runner.py` 的 `_codex_args()`。

### 5）不想执行旧评论

默认：

```env
CATCH_UP_ON_START=false
```

首次启动会跳过旧评论，只处理启动之后的新评论。
