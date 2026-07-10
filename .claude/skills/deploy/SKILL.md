---
name: deploy
description: 部署 ccmobile 到生产服务器（66.154.101.210），包含日常代码更新部署和首次完整部署流程
---

# ccmobile 部署

## 日常部署（代码更新后）

`/opt/ccmobile/` 为 root 所有，yachiyo 不能直接 scp，通过 `sudo tee` 管道写入：

```bash
# 1. 推送 server.py 到服务器（在 ccmobile/ 目录下执行）
ssh 66.154.101.210 "sudo tee /opt/ccmobile/server.py" < server.py

# 2. 重启服务并查看状态
ssh 66.154.101.210 "sudo systemctl restart ccmobile && sudo systemctl status ccmobile --no-pager"
```

## 首次部署（新服务器/重装）

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

## 生成密码哈希

```bash
python3 -c "
import hashlib, secrets
salt = secrets.token_hex(8)
password = '你的密码'
hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
print(f'sha256:{salt}:{hash_value}')
"
```

## 生产环境参考信息

- 生产地址：`https://66.154.101.210:8765`（自签名 HTTPS）
- SSH 用户：`yachiyo`（已配置在 `~/.ssh/config`，密钥 `id_ed25519`）
- 服务用户：`ccmobile`（systemd 以此用户运行，通过 sudo 调用 claude）
- Claude 运行用户：由 accounts.json 中 `linux_user` 字段指定
- 服务名：`ccmobile`（systemd 管理）
- 部署路径：`/opt/ccmobile/server.py`
- 账号配置：`/opt/ccmobile/accounts.json`（权限 600）
- systemd service 文件：仓库中的 `ccmobile.service`
- sudo 配置文件：仓库中的 `sudoers-ccmobile`
