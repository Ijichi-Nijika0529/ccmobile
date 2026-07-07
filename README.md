# CC Mobile

轻量级手机端 Claude Code 远程控制工具，使用 Deepseek v4 + Claude Code 开发。

## 功能
- 移动端 Web 终端（xterm.js）
- 多账号认证系统 + Token 会话管理
- Web 端用户注册与密码管理
- PTY 桥接 Claude Code CLI
- 多工作目录，每个目录独立 Claude 进程
- 自签名 HTTPS 支持
- 虚拟键盘（Ctrl/Alt 修饰键、QWERTY、特殊键、CLI 符号）
- 调试面板

## 部署

### 快速开始（推荐：多账号模式 + Web 注册）

```bash
# 1. 安装依赖
sudo pip3 install aiohttp

# 2. 创建服务用户
sudo useradd -r -s /bin/bash -m ccmobile

# 3. 创建普通用户组（用于共享 workspace，可选）
sudo groupadd ccmobile-users

# 4. 创建第一个普通用户
sudo useradd -m -s /bin/bash -G ccmobile-users yachiyo
sudo mkdir -p /home/yachiyo/workspace
sudo chown yachiyo:yachiyo /home/yachiyo/workspace

# 5. 部署服务文件
sudo mkdir -p /opt/ccmobile
sudo cp server.py /opt/ccmobile/
sudo chown ccmobile:ccmobile /opt/ccmobile/server.py

# 6. 创建初始账号
python3 -c "
import hashlib, secrets, json
salt = secrets.token_hex(8)
password = '你的密码'
hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
accounts = {
    'yachiyo': {
        'password_hash': f'sha256:{salt}:{hash_value}',
        'linux_user': 'yachiyo',
        'workdir': '/home/yachiyo/workspace'
    }
}
print(json.dumps(accounts, indent=2))
" | sudo tee /opt/ccmobile/accounts.json

sudo chown ccmobile:ccmobile /opt/ccmobile/accounts.json
sudo chmod 600 /opt/ccmobile/accounts.json

# 7. 配置 sudo 权限
sudo tee /etc/sudoers.d/ccmobile << 'EOF'
ccmobile ALL=(ALL:ALL) NOPASSWD: /usr/local/bin/claude
EOF
sudo chmod 440 /etc/sudoers.d/ccmobile

# 8. 迁移 Claude 配置（如有现有配置）
sudo cp -r /root/.claude /home/yachiyo/
sudo cp /root/.claude.json /home/yachiyo/
sudo chown -R yachiyo:yachiyo /home/yachiyo/.claude*

# 9. 安装并启动服务
sudo cp ccmobile.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccmobile
```

### Web 端用户管理

**注册新用户**：
1. 访问 `https://your-server:8765`
2. 点击"注册新账号"
3. 填写 username、password
4. 管理员在服务器上创建对应的 Linux 用户：
   ```bash
   sudo useradd -m -s /bin/bash -G ccmobile-users newuser
   ```

**修改密码**：
- 登录后点击"修改密码"按钮
- 输入旧密码和新密码即可

### 手动管理账号

编辑 `/opt/ccmobile/accounts.json`：
```json
{
  "alice": {
    "password_hash": "sha256:salt:hash",
    "linux_user": "alice",
    "workdir": "/home/alice/workspace"
  }
}
```

生成密码哈希：
```bash
python3 -c "
import hashlib, secrets
salt = secrets.token_hex(8)
password = 'alice的密码'
hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
print(f'sha256:{salt}:{hash_value}')
"
```

### 日常部署（代码更新后）

```bash
# 推送 server.py 到服务器
ssh your-server "sudo tee /opt/ccmobile/server.py" < server.py

# 重启服务并查看状态
ssh your-server "sudo systemctl restart ccmobile && sudo systemctl status ccmobile --no-pager"
```

## 安全特性

- **权限隔离**：服务以非特权用户 ccmobile 运行，Claude 进程以账号对应的用户权限运行
- **密码加密**：sha256 + 随机盐值，每个账号独立
- **并发安全**：accounts.json 使用 asyncio.Lock + 原子写入
- **速率限制**：
  - 登录：每 IP 每分钟最多 5 次
  - 注册：每 IP 每小时最多 3 次
  - 修改密码：每用户每小时最多 5 次
  - WebSocket：每 IP 最多 3 个并发连接，全局最多 5 个会话
- **路径遍历防护**：目录名强制只取最后一级
- **环境变量白名单**：子进程只保留安全变量
- **HTTPS 支持**：自签名证书（可选）

## 架构说明

- **多账号隔离**：每个账号映射到独立 Linux 用户，使用各自的 `~/.claude` 配置和工作目录
- **多工作目录**：同一账号可在不同目录启动独立 Claude 进程，支持多项目并行
- **PTY 桥接**：通过 `pty.fork()` + WebSocket 实现终端转发
- **会话共享**：多个客户端可以共享同一个 Claude 进程（同一目录），新连接自动重放历史输出

## 依赖
- Python 3.10+
- aiohttp
- Claude Code CLI
