# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ccmobile — 轻量级手机端 Claude Code 远程控制工具。整个应用是一个自包含的 Python 文件 `server.py`，后端用 aiohttp 提供 Web 服务和 WebSocket，前端用 xterm.js 嵌入在同一个文件里渲染终端。

同目录下还有一个 `ssh_run.py`（在上级目录 `D:\VPS\`），用于通过 Paramiko SSH 远程初始化 VPS（安装依赖、创建 swap 等），是一次性工具，不是 ccmobile 本身的一部分。

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

systemd 部署方式见 README.md。

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

- 密码验证：POST `/login` 提交 `{"password": "..."}`，成功后返回 HMAC-SHA256 token（格式 `timestamp:hash`），同时设置 httpOnly + SameSite=Strict cookie
- Token 过期时间由 `TOKEN_EXPIRE` 环境变量控制，默认 86400 秒
- 速率限制：每个 IP 在 `LOGIN_RATE_WINDOW`（60s）内最多 `LOGIN_RATE_LIMIT`（5）次登录尝试
- Origin 检查：WebSocket 连接会校验 `Origin` header 是否与 `Host` 一致，无 Origin 则放行（允许直接 IP 访问）
- 如果不设置 `CCMOBILE_PASSWORD`，认证完全跳过（开放模式，会打印 WARNING）

### 前端结构（嵌入在 INDEX_HTML 中）

- xterm.js 5.3.0 + fit 插件，从 CDN 加载
- 登录界面 → 主界面（终端 + 工具栏 + 虚拟键盘 + 输入栏）
- 工具栏提供常用快捷键按钮：Enter、^C、^D、方向键、Tab、Esc、Kill
- 虚拟键盘：QWERTY 字母行、修饰键（Ctrl/Alt/Shift/Tab 可粘滞切换）、特殊键（Esc/Tab/Spc/BS/Enter/Home/End/PgUp/PgDn/Del）、CLI 符号行
- 终端高度通过 ResizeObserver 动态计算可用空间
- 双击标题或状态文字可切换调试面板（显示最近 50 条日志）
- 备用屏幕模式下滚轮事件会批量发送，避免每条 scroll 一次 WebSocket 往返

## 配置（全部通过环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CCMOBILE_PORT` | 8765 | 监听端口 |
| `CCMOBILE_PASSWORD` | "" | 登录密码（空=开放模式） |
| `CCMOBILE_WORKDIR` | $HOME | Claude Code 工作目录 |
| `CCMOBILE_TOKEN_EXPIRE` | 86400 | Token 过期秒数 |

## 关键实现细节

- `server.py` 中 `_secret` 在模块加载时生成（`secrets.token_hex(32)`），每次重启服务器会使所有旧 token 失效
- PTY 写入通过 `_pty_write()` 处理 `BlockingIOError`，用 `loop.add_writer` 等待 fd 可写，超时 3 秒返回 False
- `kill_claude()` 先发送 Ctrl+D（`\x04`），等待 0.5s，关闭 fd，再发送 SIGTERM → 等 0.3s → SIGKILL → waitpid
- 前端 `ws.send('')` 每 10 秒发送空消息作为 keepalive，防止中间代理断开空闲连接
- 依赖：Python 3.10+（用了 `str | None` 类型注解语法）、aiohttp、系统需安装 `claude` CLI

## 安全注意事项

- `CCMOBILE_PASSWORD` 通过环境变量传入以避免硬编码在代码中。但 `spawn_claude` 中的子进程会清除这些敏感环境变量
- token cookie 设置 `httponly=True, samesite="Strict"`，但 `secure=False`（因为通常部署在内网/IP 直连，没有 TLS）。如果通过公网 HTTPS 访问，需要改为 `secure=True`
- 速率限制仅作用于 `/login` 端点，WebSocket 连接不受限（依赖 token 有效性）
- 无 CSRF 保护（所有请求走 JSON + cookie，浏览器不会自动带 JSON Content-Type 的跨域请求）
