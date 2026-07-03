# CC Mobile

轻量级手机端 Claude Code 远程控制工具。

## 功能
- 移动端 Web 终端（xterm.js）
- 密码鉴权 + Token 会话
- PTY 桥接 Claude Code CLI
- 多工作目录，每个目录独立 Claude 进程
- 虚拟键盘（Ctrl/Alt 修饰键、QWERTY、特殊键、CLI 符号）
- 调试面板

## 部署
```bash
pip3 install aiohttp

# 创建专用用户
sudo useradd -r -s /usr/sbin/nologin -d /opt/ccmobile ccmobile

# 部署服务文件
sudo cp server.py /opt/ccmobile/
sudo chown -R ccmobile:ccmobile /opt/ccmobile/

# 配置
echo 'CCMOBILE_PASSWORD=你的密码' | sudo tee /opt/ccmobile/.env
echo 'CCMOBILE_WORKDIR=/root/workspace' | sudo tee -a /opt/ccmobile/.env
sudo chown ccmobile:ccmobile /opt/ccmobile/.env
sudo chmod 600 /opt/ccmobile/.env

# Claude Code 配置（如有现有配置）
sudo cp ~/.claude.json /opt/ccmobile/ 2>/dev/null
sudo cp -r ~/.claude/ /opt/ccmobile/ 2>/dev/null
sudo chown -R ccmobile:ccmobile /opt/ccmobile/.claude* 2>/dev/null

# ccmobile 访问 /root/.claude（hooks 等）
sudo chmod o+x /root
sudo chmod -R o+rX /root/.claude 2>/dev/null

# WORKDIR 权限
sudo chown -R ccmobile:ccmobile /root/workspace

# systemd 服务
sudo cp ccmobile.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccmobile
```

## 依赖
- Python 3.10+, aiohttp
- Claude Code CLI
