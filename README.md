# CC Mobile

轻量级手机端 Claude Code 远程控制工具。

## 功能
- 移动端 Web 终端（xterm.js）
- 密码鉴权 + Token 会话
- PTY 桥接 Claude Code CLI
- 虚拟键盘（Ctrl/Alt 修饰键、QWERTY、特殊键、CLI 符号）
- 调试面板

## 部署
```bash
pip3 install aiohttp
cp server.py /opt/ccmobile/
# 配置密码
echo 'CCMOBILE_PASSWORD=你的密码' > /opt/ccmobile/.env
echo 'CCMOBILE_WORKDIR=/root/workspace' >> /opt/ccmobile/.env
# systemd 服务
sudo cp ccmobile.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccmobile
```

## 依赖
- Python 3.10+, aiohttp
- Claude Code CLI
