# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ccmobile — 轻量级手机端 Claude Code 远程控制工具。整个应用是一个自包含的 Python 文件 `server.py`，后端用 aiohttp 提供 Web 服务和 WebSocket，前端用 xterm.js 嵌入在同一个文件里渲染终端。

## 功能
- 移动端 Web 终端（xterm.js）
- 多账号认证系统 + Token 会话
- Web 端用户注册与密码管理
- PTY 桥接 Claude Code CLI
- 多工作目录，每个目录独立 Claude 进程
- 虚拟键盘（Ctrl/Alt 修饰键、QWERTY、特殊键、CLI 符号）
- 调试面板

## 开发须知

- **无测试套件**：没有 pytest、unittest 或任何自动化测试。验证修改靠手动启动服务器 + 浏览器访问 `http://localhost:8765`。
- **无 lint/格式化配置**：纯 Python 脚本，无 CI/CD。
- **依赖极简**：仅需 `aiohttp`（无 requirements.txt，直接 `pip3 install aiohttp`）。
- **Python 3.10+**（代码中使用了 `str | None` 类型注解语法）。
- **每次代码修改后必须部署到生产服务器**：push server.py 并 restart systemd 服务（部署命令见下方"日常部署"）。

## 运行与部署

```bash
# 安装依赖（仅需 aiohttp）
pip3 install aiohttp

# 密码模式运行（生产环境）
CCMOBILE_PASSWORD=你的密码 CCMOBILE_WORKDIR=/root/workspace python3 server.py

# 无密码模式（开发/调试，会打印 WARNING）
python3 server.py
```

默认监听 `0.0.0.0:8765`，可通过 `CCMOBILE_PORT` 环境变量修改。

systemd 部署文件 `ccmobile.service` 已在仓库中，可直接使用。

### 生产部署

### 日常部署（代码更新后）

`/opt/ccmobile/` 为 root 所有，yachiyo 不能直接 scp，通过 `sudo tee` 管道写入：

```bash
# 1. 推送 server.py 到服务器（在 ccmobile/ 目录下执行）
ssh 66.154.101.210 "sudo tee /opt/ccmobile/server.py" < server.py

# 2. 重启服务并查看状态
ssh 66.154.101.210 "sudo systemctl restart ccmobile && sudo systemctl status ccmobile --no-pager"
```

### 首次部署（新服务器/重装）

```bash
# 1. 创建 ccmobile 服务用户
ssh 66.154.101.210 "sudo useradd -r -s /bin/bash -m ccmobile"

# 2. 创建普通用户（用于运行 Claude）
ssh 66.154.101.210 "sudo useradd -m -s /bin/bash -G ccmobile-users yachiyo"

# 3. 配置共享 workspace（可选，如需多用户协作）
ssh 66.154.101.210 "sudo groupadd ccmobile-users"
ssh 66.154.101.210 "sudo mkdir -p /opt/workspace"
ssh 66.154.101.210 "sudo chown root:ccmobile-users /opt/workspace"
ssh 66.154.101.210 "sudo chmod 775 /opt/workspace"
ssh 66.154.101.210 "sudo chmod g+s /opt/workspace"

# 4. 创建部署目录
ssh 66.154.101.210 "sudo mkdir -p /opt/ccmobile && sudo chown ccmobile:ccmobile /opt/ccmobile"

# 5. 推送 server.py
ssh 66.154.101.210 "sudo tee /opt/ccmobile/server.py" < server.py
ssh 66.154.101.210 "sudo chown ccmobile:ccmobile /opt/ccmobile/server.py"

# 6. 安装依赖
ssh 66.154.101.210 "sudo pip3 install aiohttp"

# 7. 创建 accounts.json（多账号模式）
ssh 66.154.101.210 'sudo tee /opt/ccmobile/accounts.json' << 'EOF'
{
  "yachiyo": {
    "password_hash": "sha256:salt:hash",
    "linux_user": "yachiyo",
    "workdir": "/root/workspace"
  }
}
EOF
ssh 66.154.101.210 "sudo chown ccmobile:ccmobile /opt/ccmobile/accounts.json && sudo chmod 600 /opt/ccmobile/accounts.json"

# 8. 配置 sudo 权限（让 ccmobile 能以其他用户身份运行 claude）
ssh 66.154.101.210 "sudo tee /etc/sudoers.d/ccmobile" < sudoers-ccmobile
ssh 66.154.101.210 "sudo chmod 440 /etc/sudoers.d/ccmobile"

# 9. 迁移 Claude 配置（如有现有配置）
ssh 66.154.101.210 "sudo cp -r /root/.claude /home/yachiyo/ && sudo cp /root/.claude.json /home/yachiyo/"
ssh 66.154.101.210 "sudo chown -R yachiyo:yachiyo /home/yachiyo/.claude*"

# 10. 配置工作目录权限
ssh 66.154.101.210 "sudo chown -R yachiyo:yachiyo /root/workspace"
ssh 66.154.101.210 "sudo chmod o+x /root"  # 允许 yachiyo 遍历 /root 访问 workspace

# 11. 推送并安装 systemd 服务
ssh 66.154.101.210 "sudo tee /etc/systemd/system/ccmobile.service" < ccmobile.service
ssh 66.154.101.210 "sudo systemctl daemon-reload && sudo systemctl enable --now ccmobile"
```

- 生产地址：`https://66.154.101.210:8765`（自签名 HTTPS）
- SSH 用户：`yachiyo`（已配置在 `~/.ssh/config`，密钥 `id_ed25519`）
- 服务用户：`ccmobile`（systemd 以此用户运行，通过 sudo 调用 claude）
- Claude 运行用户：由 accounts.json 中 `linux_user` 字段指定
- 服务名：`ccmobile`（systemd 管理）
- 部署路径：`/opt/ccmobile/server.py`
- 账号配置：`/opt/ccmobile/accounts.json`（权限 600）
- systemd service 文件：仓库中的 `ccmobile.service`
- sudo 配置文件：仓库中的 `sudoers-ccmobile`

**安全说明**：
- ccmobile 服务以非特权用户 `ccmobile` 运行
- Claude 进程通过 `sudo -u <linux_user> claude` 以账号对应的用户权限启动
- 环境变量已白名单化，只保留安全变量（PATH/HOME/TERM/LANG/etc）
- WebSocket 连接有速率限制：每 IP 最多 3 个并发连接，全局最多 5 个会话
- 路径遍历防护：目录名强制只取最后一级，禁止 `../` 等路径操作
- 登录速率限制：每 IP 每分钟最多 5 次
- 注册速率限制：每 IP 每小时最多 3 次
- 密码修改速率限制：每用户每小时最多 5 次
- accounts.json 并发安全：asyncio.Lock + 原子写入

## server.py 代码导航

`server.py` 约 1050 行，用 `──` 注释分隔段落。按行号快速定位：

| 行号（约） | 段落标记 | 内容 |
|---|---|---|
| 1-16 | imports | 标准库 + aiohttp |
| 20-35 | `── config` | 环境变量读取、常量、`_secret` 生成、速率限制器 |
| 40-70 | `── token helpers` | `_hash_token()` / `make_token()` / `check_token()` / `_check_rate()` |
| 73-83 | `── Claude Code process manager` | 全局状态变量（`_claude_pid`, `_claude_fd`, `_ws_clients`, `_pty_ring` 等） |
| 86-100 | `_safe()` / `_ws_error()` | 工具函数 |
| 103-113 | `── client registry` | `_add_client()` / `_remove_client()` |
| 115-153 | `── PTY write helper` | `_pty_write()` — 非阻塞写入，BlockingIOError 用 add_writer 重试 |
| 156-185 | `── PTY window size` | `_set_winsize()` / `_apply_max_winsize()` — 多客户端取 max |
| 187-285 | `── broadcast helpers` | `_broadcast_pty()`（唯一 PTY 读协程）、`_cleanup_after_exit()` |
| 288-358 | `── Claude lifecycle` | `spawn_claude()` / `_ensure_claude()` / `kill_claude()` |
| 361-416 | `── HTTP handlers` | `handle_index()` / `handle_login()` / `handle_check()` |
| 418-527 | `── WebSocket handler` | `handle_ws()` — 鉴权→spawn→重放→消息循环 |
| 530-538 | `── app` | `web.Application` 路由绑定 |
| 539-1038 | `── embedded frontend` | `INDEX_HTML` 模板字符串（CSS + HTML + JS），内含 `// ── virtual keyboard ──` 子标记（行 934） |
| 1041-1051 | `main()` | 入口 |

## 架构核心

### PTY 桥接

- `spawn_claude()` 使用 `pty.fork()` 生成子进程运行 `claude` CLI，父进程通过 PTY fd 读写子进程的输入输出
- PTY fd 设为非阻塞模式，读用 `run_in_executor` 避免阻塞事件循环，写用 `add_writer` + await 处理 `BlockingIOError`
- `_broadcast_pty()` 是**唯一的 PTY 读取协程**，循环读取 PTY 输出并扇出到所有已连接的 WebSocket 客户端
- 当 PTY 读返回 EIO（子进程退出）或 EBADF（fd 被关闭）时，广播任务调用 `_cleanup_after_exit()` 通知所有客户端并重置状态

### 多客户端共享

- **一个 Claude 进程被所有 WebSocket 客户端共享**。客户端断开连接不会杀死 Claude，只会从 `_ws_clients` 集合中移除
- 只有 WebSocket 客户端发送 `{"type": "kill"}` 控制消息时才会杀死 Claude
- 新客户端连接时会重放 PTY 环形缓冲区的历史输出（最多 10MB），让新客户端看到完整上下文
- `_claude_lock`（asyncio.Lock）保护 spawn/kill 操作，防止竞态
- WebSocket handler 在连接时会检测 `_claude_lock` 是否卡死（前一个 spawn/kill 挂起），如有则清理后重试

### 认证与安全

**多账号模式**（accounts.json 存在时）：
- POST `/login` 提交 `{"username": "alice", "password": "..."}`
- 密码验证：读取 `/opt/ccmobile/accounts.json`，校验 `sha256:salt:hash` 格式的密码哈希
- Token 格式：`username:timestamp:hmac`（HMAC 签名包含 username，防止伪造）
- 每个账号映射到独立的 Linux 用户，Claude 进程以对应用户身份运行（`sudo -u <linux_user> claude`）
- 配置文件隔离：每个用户使用自己的 `~/.claude.json` 和工作目录

**单密码模式**（向后兼容，无 accounts.json 时）：
- POST `/login` 提交 `{"password": "..."}`
- 校验 `CCMOBILE_PASSWORD` 环境变量
- Token 格式：`:timestamp:hmac`（空 username）
- Claude 进程以 `CCMOBILE_CLAUDE_USER` 指定的用户运行（如设置）

**通用安全机制**：
- Token 过期时间：`TOKEN_EXPIRE` 环境变量控制，默认 86400 秒
- Token cookie：httpOnly + SameSite=Strict（secure=False，内网部署）
- 速率限制：每个 IP 在 60s 内最多 5 次登录尝试
- Origin 检查：WebSocket 连接校验 `Origin` header 与 `Host` 一致
- 前端自动检测认证模式：调用 `/api/auth-mode` 端点，决定显示单密码框或 username+password

### WebSocket 协议消息

连接建立后，客户端与服务器之间通过以下 JSON 消息通信：

| 方向 | type | payload | 触发场景 |
|---|---|---|---|
| Client→Server | `{"type":"resize",cols,rows}` | 终端网格尺寸 | 窗口 resize / fit 后 |
| Client→Server | `{"type":"kill"}` | 无 | 用户点击 Kill 按钮 |
| Server→Client | `{"type":"ready"}` | 无 | Claude 启动完毕，前端隐藏 Start overlay |
| Server→Client | `{"type":"exited"}` | 无 | Claude 进程退出，前端显示 Start overlay |
| Server→Client | `{"type":"size",cols,rows}` | PTY winsize | `_apply_max_winsize()` 广播，客户端同步 `term.resize()` |
| Server→Client | `{"type":"error","msg":"..."}` | 错误描述 | WebSocket 握手阶段（鉴权失败/启动失败） |

- 非 JSON 的字符串和二进制消息直接写入 PTY（透传给 Claude）
- 空消息（`""`）被前端每 10 秒发送一次作为 keepalive，服务器端 `if not data: continue` 忽略

### 前端结构（嵌入在 INDEX_HTML 中）

- xterm.js 5.3.0 + fit 插件，从 CDN 加载
- 登录界面 → 主界面（终端 + 工具栏 + 虚拟键盘 + 输入栏）
- 工具栏提供常用快捷键按钮：Enter、^C、^D、方向键、Tab、Esc、Kill
- 虚拟键盘：QWERTY 字母行、修饰键（Ctrl/Alt/Shift/Tab 可粘滞切换）、特殊键（Esc/Tab/Spc/BS/Enter/Home/End/PgUp/PgDn/Del）、CLI 符号行
- 终端尺寸：CSS flexbox（`#terminal-container` flex:1）+ FitAddon 计算 cols/rows。客户端用双 `requestAnimationFrame` 延迟首次 fit（避免容器未布局时高度为 0）
- PTY 窗口大小传播：客户端发送 `{"type":"resize",cols,rows}`，服务器在所有客户端中**取最大**尺寸，通过 `TIOCSWINSZ` 设置 PTY（内核发 SIGWINCH 触发 Claude 重渲染），并广播 `{"type":"size"}` 令所有客户端网格与 PTY 一致；小屏客户端网格大于视口时用 CSS（`.xterm` 绝对定位 left:0;bottom:0）裁剪显示左下角（最新内容）
- 双击标题或状态文字可切换调试面板（显示最近 50 条日志）
- 备用屏幕模式下滚轮事件会批量发送，避免每条 scroll 一次 WebSocket 往返

## 配置（全部通过环境变量或配置文件）

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CCMOBILE_PORT` | 8765 | 监听端口 |
| `CCMOBILE_PASSWORD` | "" | 单密码模式：登录密码（空=开放模式）。多账号模式下忽略 |
| `CCMOBILE_WORKDIR` | $HOME | 单密码模式：Claude Code 工作目录。多账号模式下忽略 |
| `CCMOBILE_TOKEN_EXPIRE` | 86400 | Token 过期秒数 |
| `CCMOBILE_CLAUDE_USER` | "" | 单密码模式：通过 `sudo -u <user> claude` 启动 Claude。多账号模式下忽略 |

### accounts.json（多账号模式）

位置：`/opt/ccmobile/accounts.json`（生产）或 `./accounts.json`（本地开发）

格式：
```json
{
  "alice": {
    "password_hash": "sha256:salt:hash",
    "linux_user": "alice",
    "workdir": "/home/alice/workspace"
  }
}
```

- `password_hash`：格式 `sha256:salt:hash`，其中 `hash = sha256(salt + password)`
- `linux_user`：对应的 Linux 用户名（Claude 进程以此用户身份运行）
- `workdir`：默认工作目录，可选

**权限**：
```bash
sudo chown ccmobile:ccmobile /opt/ccmobile/accounts.json
sudo chmod 600 /opt/ccmobile/accounts.json
```

**生成密码哈希**：
```bash
python3 -c "
import hashlib, secrets
salt = secrets.token_hex(8)
password = '你的密码'
hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
print(f'sha256:{salt}:{hash_value}')
"
```

**Web 端注册**：
- 访问登录页面，点击"注册新账号"
- 填写 username、password
- 提交后 accounts.json 自动更新
- **注意**：管理员需先创建对应的 Linux 用户：
  ```bash
  sudo useradd -m -s /bin/bash -G ccmobile-users newuser
  ```

**修改密码**：
- 登录后点击"修改密码"按钮
- 输入旧密码和新密码
- 提交后 accounts.json 自动更新

## 关键实现细节

- `server.py` 中 `_secret` 在模块加载时生成（`secrets.token_hex(32)`），每次重启服务器会使所有旧 token 失效
- PTY 写入通过 `_pty_write()` 处理 `BlockingIOError`，用 `loop.add_writer` 等待 fd 可写，超时 3 秒返回 False
- `kill_claude()` 先发送 Ctrl+D（`\x04`），等待 0.5s，关闭 fd，再发送 SIGTERM → 等 0.3s → SIGKILL → waitpid
- 前端 `ws.send('')` 每 10 秒发送空消息作为 keepalive，防止中间代理断开空闲连接
- 依赖：Python 3.10+（用了 `str | None` 类型注解语法）、aiohttp、系统需安装 `claude` CLI
- **日志前缀约定**：所有 `print()` 使用 `[ccmobile]` / `[ws]` / `[login]` 前缀，新增日志应遵循此约定
- `WS_MSG_MAX`（1MB）和 `WS_IDLE_TIMEOUT`（30min）当前为模块级硬编码常量，如需调整可直接修改 `server.py` 顶部常量区
- **多账号实现**：
  - `_load_accounts()` 启动时读取 accounts.json，存入 `_accounts` 全局变量
  - Token 格式：`username:timestamp:hmac`，HMAC 签名包含 username
  - Session 包含 `linux_user` 字段，spawn_claude 时用 `sudo -u <linux_user> claude`
  - handle_dirs/handle_mkdir 根据 token 中的 username 返回对应用户的 workdir
  - 前端调用 `/api/auth-mode` 检测模式，动态显示登录表单
- **用户注册与密码管理**：
  - `/api/register`：Web 端自助注册，写入 accounts.json
  - `/api/change-password`：已登录用户修改密码
  - `_save_accounts()`：asyncio.Lock + 原子写入（临时文件 + os.replace）
  - 速率限制：注册 3 次/小时/IP，修改密码 5 次/小时/用户
  - 注册后需管理员手动创建 Linux 用户
- **安全加固**：
  - 环境变量白名单：子进程只保留 PATH/HOME/TERM/LANG/LC_ALL/USER/LOGNAME/SHELL，清除所有其他变量（防凭证泄漏）
  - 路径遍历防护：`_get_or_create_session()` 强制只取目录名最后一级（`Path(dirname).name`），禁止 `../` 等路径操作
  - WebSocket 速率限制：每 IP 最多 `WS_PER_IP_LIMIT`（3）个并发连接，全局最多 `MAX_SESSIONS`（5）个活跃会话
  - 权限隔离：systemd 以非特权用户 `ccmobile` 运行服务，通过 `sudo -u` 以目标用户权限启动 Claude 子进程
  - 僵尸进程防护：`kill_claude()` 用 `os.waitpid(pid, os.WNOHANG)` 正确回收子进程，防止僵尸堆积
  - 密码哈希：多账号模式使用 sha256+salt，盐值每个账号独立
  - accounts.json 权限必须是 600（仅 ccmobile 可读）

## 待办 / 预留功能

- **账号管理 CLI**：当前手动编辑 accounts.json，未来可添加 `server.py adduser/deluser/passwd` 命令

## 安全注意事项

- **多账号模式**：
  - `accounts.json` 权限必须是 `600`（仅 ccmobile 可读）
  - sudoers 配置必须使用绝对路径 `/usr/local/bin/claude`，防止路径劫持
  - 密码哈希使用 sha256+salt（内网部署已足够），盐值每个账号独立
- **单密码模式**：
  - `CCMOBILE_PASSWORD` 通过环境变量传入以避免硬编码在代码中。但 `spawn_claude` 中的子进程会清除这些敏感环境变量
- **通用**：
  - token cookie 设置 `httponly=True, samesite="Strict"`，但 `secure=False`（因为通常部署在内网/IP 直连，没有 TLS）。如果通过公网 HTTPS 访问，需要改为 `secure=True`
  - 速率限制仅作用于 `/login` 端点，WebSocket 连接不受限（依赖 token 有效性）
  - 无 CSRF 保护（所有请求走 JSON + cookie，浏览器不会自动带 JSON Content-Type 的跨域请求）
