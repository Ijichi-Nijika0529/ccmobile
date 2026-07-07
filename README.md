# CC Mobile

轻量级手机端 Claude Code 远程控制工具，使用Deepseekv4+ClaudeCode开发。

## 功能
- 移动端 Web 终端（xterm.js）
- 多账号认证（每个账号映射到独立 Linux 用户，隔离配置文件）
- Token 会话管理
- PTY 桥接 Claude Code CLI
- 多工作目录，每个目录独立 Claude 进程
- 虚拟键盘（Ctrl/Alt 修饰键、QWERTY、特殊键、CLI 符号）
- 调试面板

## 部署

### 单密码模式（向后兼容）

```bash
pip3 install aiohttp

# 创建专用用户
sudo useradd -r -s /bin/bash -m ccmobile

# 部署服务文件
sudo cp server.py /opt/ccmobile/
sudo chown -R ccmobile:ccmobile /opt/ccmobile/

# 配置
echo 'CCMOBILE_PASSWORD=你的密码' | sudo tee /opt/ccmobile/.env
echo 'CCMOBILE_WORKDIR=/root/workspace' | sudo tee -a /opt/ccmobile/.env
echo 'CCMOBILE_CLAUDE_USER=yachiyo' | sudo tee -a /opt/ccmobile/.env
sudo chown ccmobile:ccmobile /opt/ccmobile/.env
sudo chmod 600 /opt/ccmobile/.env

# sudo 权限（让 ccmobile 能以指定用户运行 claude）
sudo tee /etc/sudoers.d/ccmobile << 'EOF'
ccmobile ALL=(yachiyo) NOPASSWD: /usr/local/bin/claude
EOF
sudo chmod 440 /etc/sudoers.d/ccmobile

# WORKDIR 权限
sudo chown -R ccmobile:ccmobile /root/workspace

# systemd 服务
sudo cp ccmobile.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccmobile
```

### 多账号模式（推荐）

```bash
pip3 install aiohttp

# 创建专用用户
sudo useradd -r -s /bin/bash -m ccmobile

# 部署服务文件
sudo cp server.py /opt/ccmobile/
sudo chown -R ccmobile:ccmobile /opt/ccmobile/

# 创建 Linux 用户（每个 ccmobile 账号对应一个）
sudo useradd -m -s /bin/bash alice
sudo mkdir -p /home/alice/workspace
sudo chown alice:alice /home/alice/workspace

sudo useradd -m -s /bin/bash bob
sudo mkdir -p /home/bob/workspace
sudo chown bob:bob /home/bob/workspace

# 生成密码哈希（在本地 Windows 上运行）
python3 -c "
import hashlib, secrets
salt = secrets.token_hex(8)
password = 'alice的密码'
hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
print(f'sha256:{salt}:{hash_value}')
"

# 创建 accounts.json
sudo tee /opt/ccmobile/accounts.json << 'EOF'
{
  "alice": {
    "password_hash": "sha256:盐值:哈希值",
    "linux_user": "alice",
    "workdir": "/home/alice/workspace"
  },
  "bob": {
    "password_hash": "sha256:盐值:哈希值",
    "linux_user": "bob",
    "workdir": "/home/bob/workspace"
  }
}
EOF
sudo chown ccmobile:ccmobile /opt/ccmobile/accounts.json
sudo chmod 600 /opt/ccmobile/accounts.json

# sudo 权限（让 ccmobile 能以任意用户运行 claude）
sudo tee /etc/sudoers.d/ccmobile << 'EOF'
ccmobile ALL=(ALL:ALL) NOPASSWD: /usr/local/bin/claude
EOF
sudo chmod 440 /etc/sudoers.d/ccmobile

# systemd 服务
sudo cp ccmobile.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccmobile
```

### 日常部署（代码更新后）

```bash
# 推送 server.py 到服务器
ssh your-server "sudo tee /opt/ccmobile/server.py" < server.py

# 重启服务
ssh your-server "sudo systemctl restart ccmobile && sudo systemctl status ccmobile --no-pager"
```

## 依赖
- Python 3.10+, aiohttp
- Claude Code CLI
