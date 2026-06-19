#!/usr/bin/env python3
"""ccmobile — lightweight mobile remote control for Claude Code."""

import asyncio
import hashlib
import errno
import json
import os
import pty
import secrets
import termios
import time
from pathlib import Path

from aiohttp import web

# ── config ──────────────────────────────────────────────────────────
PORT = int(os.environ.get("CCMOBILE_PORT", "8765"))
PASSWORD = os.environ.get("CCMOBILE_PASSWORD", "")
WORKDIR = os.environ.get("CCMOBILE_WORKDIR", str(Path.home()))
TOKEN_EXPIRE = int(os.environ.get("CCMOBILE_TOKEN_EXPIRE", "86400"))

_secret = secrets.token_hex(32)


# ── token helpers ────────────────────────────────────────────────────

def make_token() -> str:
    ts = str(int(time.time()))
    h = hashlib.sha256(f"{PASSWORD}:{ts}:{_secret}".encode()).hexdigest()[:32]
    return f"{ts}:{h}"


def check_token(token: str) -> bool:
    if not token:
        return False
    try:
        ts_str, _ = token.split(":", 1)
        if time.time() - int(ts_str) > TOKEN_EXPIRE:
            return False
        expected = hashlib.sha256(f"{PASSWORD}:{ts_str}:{_secret}".encode()).hexdigest()[:32]
        return secrets.compare_digest(token.split(":", 2)[1], expected)
    except (ValueError, AttributeError):
        return False


# ── Claude Code process manager ─────────────────────────────────────

_claude_pid: int | None = None
_claude_fd: int | None = None
_lock = asyncio.Lock()


async def spawn_claude() -> int:
    global _claude_pid, _claude_fd
    Path(WORKDIR).mkdir(parents=True, exist_ok=True)

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(WORKDIR)
        os.execvp("claude", ["claude"])
        os._exit(127)
    else:
        _claude_pid = pid
        _claude_fd = fd
        os.set_blocking(fd, False)
        return fd


async def kill_claude():
    global _claude_pid, _claude_fd
    pid, fd = _claude_pid, _claude_fd

    if fd is not None:
        try:
            os.write(fd, b"\x04")
            await asyncio.sleep(0.5)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _claude_fd = None

    if pid is not None:
        try:
            os.kill(pid, 15)
            await asyncio.sleep(0.3)
            os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        _claude_pid = None


# ── HTTP handlers ────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")


async def handle_login(request: web.Request) -> web.Response:
    if not PASSWORD:
        return web.json_response({"token": make_token(), "expires": TOKEN_EXPIRE})
    try:
        body = await request.json()
        pw = body.get("password", "")
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400)
    if pw != PASSWORD:
        await asyncio.sleep(1)
        return web.json_response({"error": "wrong password"}, status=403)
    return web.json_response({"token": make_token(), "expires": TOKEN_EXPIRE})


async def handle_check(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not PASSWORD:
        return web.json_response({"valid": True})
    return web.json_response({"valid": check_token(token)})


# ── WebSocket handler ────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    token = request.query.get("token", "")
    peer = request.remote or "?"
    print(f"[ws] connect from {peer}")

    if PASSWORD and not check_token(token):
        print(f"[ws] {peer} bad token")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "error", "msg": "invalid token"}))
        await ws.close()
        return ws

    if _lock.locked():
        print(f"[ws] {peer} rejected - lock held")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "error", "msg": "Claude Code is already running"}))
        await ws.close()
        return ws

    async with _lock:
        await kill_claude()
        print(f"[ws] {peer} spawning Claude...")

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        try:
            fd = await spawn_claude()
        except FileNotFoundError:
            print(f"[ws] {peer} claude not found")
            await ws.send_str(json.dumps({"type": "error", "msg": "claude CLI not found"}))
            await ws.close()
            return ws
        except Exception as e:
            print(f"[ws] {peer} spawn error: {e}")
            await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))
            await ws.close()
            return ws

        print(f"[ws] {peer} Claude PID={_claude_pid} fd={fd}")
        await ws.send_str(json.dumps({"type": "ready"}))

        byte_count = 0
        msg_count = 0

        async def pty_to_ws():
            nonlocal byte_count
            loop = asyncio.get_running_loop()
            try:
                while True:
                    try:
                        data = await loop.run_in_executor(None, os.read, fd, 65536)
                        if not data:
                            break
                        byte_count += len(data)
                        await ws.send_bytes(data)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            break
                        await asyncio.sleep(0.05)
                    except asyncio.CancelledError:
                        break
            finally:
                print(f"[ws] {peer} PTY done ({byte_count} bytes)")
                try:
                    await ws.send_str(json.dumps({"type": "exited"}))
                except Exception:
                    pass

        reader_task = asyncio.create_task(pty_to_ws())

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    msg_count += 1
                    os.write(fd, msg.data.encode())
                elif msg.type == web.WSMsgType.BINARY:
                    msg_count += 1
                    os.write(fd, msg.data)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        finally:
            print(f"[ws] {peer} session end ({msg_count} msgs, {byte_count} bytes)")
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            await kill_claude()
            await ws.close()

        return ws


# ── app ─────────────────────────────────────────────────────────────

app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_post("/login", handle_login)
app.router.add_get("/check", handle_check)
app.router.add_get("/ws", handle_ws)


# ── embedded frontend ────────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<title>CC Mobile</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark;--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#c9d1d9;--accent:#58a6ff;--danger:#f85149;--green:#3fb950;--warn:#d29922}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:var(--text);display:flex;flex-direction:column;-webkit-tap-highlight-color:transparent}
#login-screen{display:none;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:24px;gap:20px}
#login-screen h1{font-size:22px;font-weight:600}
#login-screen input{width:100%;max-width:320px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:16px;outline:none}
#login-screen input:focus{border-color:var(--accent)}
#login-btn{padding:12px 28px;background:var(--accent);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;width:100%;max-width:320px}
#login-btn:active{opacity:.7}
#login-error{color:var(--danger);font-size:14px;min-height:20px}
#main-screen{display:none;flex-direction:column;height:100%}
#status-bar{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:var(--surface);border-bottom:1px solid var(--border);font-size:11px;flex-shrink:0}
#status-dot{width:8px;height:8px;border-radius:50%;background:var(--danger);flex-shrink:0}
#status-dot.on{background:var(--green)}
#status-left{display:flex;align-items:center;gap:6px;flex:1;min-width:0}
#status-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#logout-btn{font-size:10px;background:var(--border);color:var(--text);border:none;padding:4px 10px;border-radius:5px;cursor:pointer}
#terminal-container{flex:1;padding:2px;overflow:hidden;min-height:0;user-select:text;-webkit-user-select:text}
#terminal-container .xterm{height:100%;user-select:text;-webkit-user-select:text}
.xterm-viewport::-webkit-scrollbar{width:4px}
.xterm-viewport::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
#start-overlay{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;background:var(--bg);gap:12px}
#start-overlay p{font-size:13px;color:var(--text);opacity:.6}
#start-btn{padding:16px 36px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-size:17px;font-weight:600;cursor:pointer}
#start-btn:active{opacity:.7}
#toolbar{display:flex;gap:4px;padding:6px 8px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0;justify-content:center;flex-wrap:wrap}
.tb-btn{padding:8px 10px;font-size:12px;border-radius:6px;text-align:center;border:none;font-weight:600;cursor:pointer;color:#fff}
.tb-btn:active{opacity:.7}
.tb-accent{background:var(--accent)}
.tb-danger{background:var(--danger)}
.tb-gray{background:var(--border);color:var(--text)}
.tb-enter{background:var(--green);flex:2;max-width:120px}
#input-row{display:flex;gap:4px;padding:4px 8px 8px;background:var(--surface);flex-shrink:0}
#input-row input{flex:1;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;outline:none;min-width:0}
#input-row input:focus{border-color:var(--accent)}
#send-btn{padding:10px 16px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;flex-shrink:0}
#send-btn:active{opacity:.7}
#debug-panel{display:none;background:#000;color:var(--warn);font-size:10px;padding:4px 8px;max-height:80px;overflow-y:auto;flex-shrink:0;font-family:monospace;border-top:1px solid var(--border)}
/* virtual keyboard */
#vk-panel{display:none;flex-shrink:0;background:var(--surface);border-top:1px solid var(--border);padding:3px 4px;max-height:45vh;overflow-y:auto}
.vk-row{display:flex;gap:2px;justify-content:center;flex-wrap:wrap;margin:1px 0}
.vk-btn{min-width:26px;height:30px;padding:3px 5px;font-size:11px;border-radius:4px;border:none;font-weight:600;cursor:pointer;color:var(--text);background:var(--border);text-align:center;line-height:24px}
.vk-btn:active{opacity:.7}
.vk-btn.mod{min-width:42px;font-size:10px;border-radius:5px}
.vk-mod-ctrl{background:#1a3a5c;color:#58a6ff}
.vk-mod-alt{background:#2d1a3c;color:#bc8cff}
.vk-mod-shift{background:#1a3c2d;color:#3fb950}
.vk-mod-ctrl.on{background:#58a6ff;color:#fff;outline:2px solid #80bfff}
.vk-mod-alt.on{background:#bc8cff;color:#fff;outline:2px solid #d2a8ff}
.vk-mod-shift.on{background:#3fb950;color:#fff;outline:2px solid #70d970}
.vk-mod-tab{background:#1a3c3c;color:#56d4dd}
.vk-mod-tab.on{background:#56d4dd;color:#000;outline:2px solid #80e8e8}
.vk-btn.sym{background:#1a2a1a;color:#7ee787}
.vk-btn.special{background:#1a1a2e;color:#a0a0d0}
#btn-kbd{background:var(--warn);color:#000}
</style>
</head>
<body>

<div id="login-screen">
  <h1>CC Mobile</h1>
  <input id="pw-input" type="password" placeholder="Password" enterkeyhint="go" autocomplete="off">
  <button id="login-btn">Connect</button>
  <span id="login-error"></span>
</div>

<div id="main-screen">
  <div id="status-bar">
    <div id="status-left">
      <span id="status-dot"></span>
      <span id="status-text">offline</span>
    </div>
    <button id="logout-btn">Logout</button>
  </div>
  <div id="terminal-container">
    <div id="start-overlay">
      <button id="start-btn">Start Claude Code</button>
      <p>Terminal ready</p>
    </div>
  </div>
  <div id="toolbar">
    <button class="tb-btn tb-enter" id="btn-enter">Enter &crarr;</button>
    <button class="tb-btn tb-accent" id="btn-c">^C</button>
    <button class="tb-btn tb-accent" id="btn-d">^D</button>
    <button class="tb-btn tb-gray" id="btn-left">&larr;</button>
    <button class="tb-btn tb-gray" id="btn-up">&uarr;</button>
    <button class="tb-btn tb-gray" id="btn-down">&darr;</button>
    <button class="tb-btn tb-gray" id="btn-right">&rarr;</button>
    <button class="tb-btn tb-gray" id="btn-tab">Tab</button>
    <button class="tb-btn tb-gray" id="btn-esc">Esc</button>
    <button class="tb-btn" id="btn-kbd">Kbd</button>
    <button class="tb-btn tb-danger" id="btn-kill">Kill</button>
  </div>
  <div id="vk-panel">
    <div class="vk-row" id="vk-row-mod">
      <button class="vk-btn mod vk-mod-ctrl" data-mod="ctrl">Ctrl</button>
      <button class="vk-btn mod vk-mod-alt" data-mod="alt">Alt</button>
      <button class="vk-btn mod vk-mod-shift" data-mod="shift">Shift</button>
      <button class="vk-btn mod vk-mod-tab" data-mod="vt">Tab</button>
      <span style="flex:1;min-width:4px"></span>
      <button class="vk-btn special" data-key="esc">Esc</button>
      <button class="vk-btn special" data-key="tab">Tab</button>
      <button class="vk-btn special" data-key="spc">Spc</button>
      <button class="vk-btn special" data-key="bs">BS</button>
      <button class="vk-btn special" data-key="enter">Ent</button>
      <button class="vk-btn special" data-key="home">Hm</button>
      <button class="vk-btn special" data-key="end">End</button>
      <button class="vk-btn special" data-key="pgup">Pu</button>
      <button class="vk-btn special" data-key="pgdn">Pd</button>
      <button class="vk-btn special" data-key="del">Del</button>
    </div>
    <div class="vk-row" id="vk-row-q1">
      <button class="vk-btn" data-key="q">Q</button><button class="vk-btn" data-key="w">W</button><button class="vk-btn" data-key="e">E</button><button class="vk-btn" data-key="r">R</button><button class="vk-btn" data-key="t">T</button><button class="vk-btn" data-key="y">Y</button><button class="vk-btn" data-key="u">U</button><button class="vk-btn" data-key="i">I</button><button class="vk-btn" data-key="o">O</button><button class="vk-btn" data-key="p">P</button>
    </div>
    <div class="vk-row" id="vk-row-q2">
      <button class="vk-btn" data-key="a">A</button><button class="vk-btn" data-key="s">S</button><button class="vk-btn" data-key="d">D</button><button class="vk-btn" data-key="f">F</button><button class="vk-btn" data-key="g">G</button><button class="vk-btn" data-key="h">H</button><button class="vk-btn" data-key="j">J</button><button class="vk-btn" data-key="k">K</button><button class="vk-btn" data-key="l">L</button>
    </div>
    <div class="vk-row" id="vk-row-q3">
      <button class="vk-btn" data-key="z">Z</button><button class="vk-btn" data-key="x">X</button><button class="vk-btn" data-key="c">C</button><button class="vk-btn" data-key="v">V</button><button class="vk-btn" data-key="b">B</button><button class="vk-btn" data-key="n">N</button><button class="vk-btn" data-key="m">M</button>
    </div>
    <div class="vk-row" id="vk-row-sym">
      <button class="vk-btn sym" data-key="/">/</button><button class="vk-btn sym" data-key=".">.</button><button class="vk-btn sym" data-key="-">-</button><button class="vk-btn sym" data-key="_">_</button><button class="vk-btn sym" data-key="$">$</button><button class="vk-btn sym" data-key="#">#</button><button class="vk-btn sym" data-key="|">|</button><button class="vk-btn sym" data-key=">">></button><button class="vk-btn sym" data-key="~">~</button><button class="vk-btn sym" data-key="&">&amp;</button><button class="vk-btn sym" data-key=";">;</button><button class="vk-btn sym" data-key="*">*</button>
    </div>
  </div>
  <div id="input-row">
    <input id="msg-input" type="text" placeholder="Message..." enterkeyhint="send" autocomplete="off" autocapitalize="none">
    <button id="send-btn">Send</button>
  </div>
  <div id="debug-panel"></div>
</div>

<script>
const dbg = [];
function log(tag, msg) {
  const ts = new Date().toLocaleTimeString();
  dbg.push(`[${ts}] ${tag}: ${msg}`);
  if (dbg.length > 50) dbg.shift();
  const panel = document.getElementById('debug-panel');
  if (panel.style.display !== 'none') panel.textContent = dbg.join('\n');
}
window.onerror = (msg, src, line) => {
  log('JS ERROR', msg + ' at ' + (src||'?').split('/').pop() + ':' + line);
};

let token = localStorage.getItem('ccmobile_token') || '';
let ws = null, term = null, fit = null, byteCount = 0;

const $ = id => document.getElementById(id);
const loginScreen = $('login-screen'), mainScreen = $('main-screen');
const pwInput = $('pw-input'), loginBtn = $('login-btn'), loginError = $('login-error');
const statusDot = $('status-dot'), statusText = $('status-text');
const terminalContainer = $('terminal-container');
const startOverlay = $('start-overlay'), startBtn = $('start-btn');
const msgInput = $('msg-input'), sendBtn = $('send-btn');

async function tryLogin(pw) {
  log('AUTH', 'logging in...');
  const res = await fetch('/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pw})
  });
  const data = await res.json();
  if (res.ok) {
    token = data.token;
    localStorage.setItem('ccmobile_token', token);
    log('AUTH', 'OK, expires in ' + data.expires + 's');
    return true;
  }
  throw new Error(data.error || 'Login failed');
}

async function checkToken() {
  if (!token) return false;
  const res = await fetch('/check?token=' + encodeURIComponent(token));
  const data = await res.json();
  log('AUTH', 'token check: ' + data.valid);
  return data.valid;
}

async function doLogin() {
  loginError.textContent = '';
  try {
    if (await tryLogin(pwInput.value)) showMain();
  } catch (e) {
    loginError.textContent = e.message;
    log('AUTH', 'FAIL: ' + e.message);
  }
}

pwInput.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
loginBtn.addEventListener('click', doLogin);

function initTerminal() {
  log('TERM', 'initializing xterm.js...');
  try {
    term = new Terminal({
      cursorBlink: true, cursorStyle: 'bar', fontSize: 13,
      fontFamily: "'JetBrains Mono','Fira Code','Cascadia Code',Menlo,monospace",
      theme: {
        background: '#0d1117', foreground: '#c9d1d9',
        cursor: '#58a6ff', selectionBackground: '#264f78',
        black: '#484f58', red: '#ff7b72', green: '#3fb950', yellow: '#d29922',
        blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#b1bac4',
        brightBlack: '#6e7681', brightRed: '#ffa198', brightGreen: '#56d364',
        brightYellow: '#e3b341', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
        brightCyan: '#56d4dd', brightWhite: '#f0f6fc'
      },
      scrollback: 5000
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(terminalContainer);
    fit.fit();
    window.addEventListener('resize', () => { try { fit.fit(); } catch(e){} });
    log('TERM', 'OK, cols=' + term.cols + ' rows=' + term.rows);
  } catch(e) {
    log('TERM', 'FAIL: ' + e.message);
  }
}

function setStatus(on, text) {
  statusDot.className = on ? 'on' : '';
  statusText.textContent = text;
  log('STATUS', text);
}

function connectWS() {
  return new Promise((resolve, reject) => {
    const url = (location.protocol==='https:'?'wss':'ws') + '://' + location.host + '/ws?token=' + encodeURIComponent(token);
    log('WS', 'connecting...');
    try { ws = new WebSocket(url); } catch(e) { log('WS', 'FAIL: '+e.message); reject(e); return; }
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      setStatus(true, 'connected');
      log('WS', 'open');
    };

    ws.onmessage = e => {
      if (typeof e.data === 'string') {
        try {
          const m = JSON.parse(e.data);
          if (m.type === 'ready') {
            setStatus(true, 'Claude Code running');
            startOverlay.style.display = 'none';
            try { fit.fit(); } catch(_){}
            log('WS', 'Claude Code READY');
            resolve();
          } else if (m.type === 'exited') {
            setStatus(false, 'exited');
            log('WS', 'Claude exited');
            term.clear();
            startOverlay.style.display = 'flex';
          } else if (m.type === 'error') {
            setStatus(false, m.msg);
            log('WS', 'ERROR: ' + m.msg);
            term.writeln('\r\n\x1b[31m' + m.msg + '\x1b[0m');
            reject(new Error(m.msg));
          }
        } catch (_) {
          term.write(e.data);
          log('WS', 'text ' + (e.data.length > 50 ? e.data.substr(0,50)+'...' : e.data));
        }
      } else {
        byteCount += (e.data instanceof ArrayBuffer ? e.data.byteLength : 0);
        term.write(new Uint8Array(e.data));
      }
    };

    ws.onclose = () => {
      setStatus(false, 'disconnected');
      log('WS', 'closed after ' + byteCount + ' bytes received');
      ws = null;
    };

    ws.onerror = () => {
      log('WS', 'onerror');
      setStatus(false, 'error');
      reject(new Error('WebSocket error'));
    };
  });
}

async function startClaude() {
  byteCount = 0;
  startBtn.disabled = true;
  startBtn.textContent = 'Starting...';
  try { await connectWS(); } catch (e) {
    log('START', 'FAIL: ' + e.message);
    startBtn.disabled = false;
    startBtn.textContent = 'Start Claude Code';
  }
}
startBtn.addEventListener('click', startClaude);

function sendMsg() {
  const text = msgInput.value;
  if (!text) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    log('SEND', 'WS not open');
    return;
  }
  ws.send(text + '\r');
  log('SEND', text);
  msgInput.value = '';
  sendBtn.textContent = 'Sent!';
  setTimeout(() => { sendBtn.textContent = 'Send'; }, 300);
}
msgInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); sendMsg(); }
});
sendBtn.addEventListener('click', sendMsg);

function wsSend(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(data);
    log('SEND', '0x' + data.charCodeAt(0).toString(16));
  }
}
$('btn-enter').addEventListener('click', () => wsSend('\r'));
$('btn-c').addEventListener('click', () => wsSend('\x03'));
$('btn-d').addEventListener('click', () => wsSend('\x04'));
$('btn-left').addEventListener('click', () => wsSend('\x1b[D'));
$('btn-up').addEventListener('click', () => wsSend('\x1b[A'));
$('btn-down').addEventListener('click', () => wsSend('\x1b[B'));
$('btn-right').addEventListener('click', () => wsSend('\x1b[C'));
$('btn-tab').addEventListener('click', () => wsSend('\t'));
$('btn-esc').addEventListener('click', () => wsSend('\x1b'));
$('btn-kill').addEventListener('click', () => { if (ws) ws.close(); log('KILL', 'forced'); });

// ── virtual keyboard ──
const vkMod = { ctrl: false, alt: false, shift: false, vt: false };
const KEY_MAP = {
  esc: '\x1b', tab: '\t', spc: ' ', bs: '\x7f', enter: '\r',
  home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~', del: '\x1b[3~',
  '/': '/', '.': '.', '-': '-', '_': '_', '$': '$', '#': '#', '|': '|', '>': '>', '~': '~', '&': '&', ';': ';', '*': '*'
};

function vkUpdateModUI() {
  $('vk-row-mod').querySelectorAll('.mod').forEach(b => {
    b.classList.toggle('on', vkMod[b.dataset.mod]);
  });
}

function vkSend(key) {
  let data = KEY_MAP[key];
  if (data !== undefined) { wsSend(data); return; }
  if (!/[a-z]/i.test(key)) return;
  const lower = key.toLowerCase(), code = lower.charCodeAt(0);
  let prefix = '';
  if (vkMod.vt) { prefix = '\t'; vkMod.vt = false; }
  if (vkMod.ctrl) { wsSend(prefix + String.fromCharCode(code - 96)); vkMod.ctrl = false; }
  else if (vkMod.alt) { wsSend(prefix + '\x1b' + lower); vkMod.alt = false; }
  else if (vkMod.shift) { wsSend(prefix + key.toUpperCase()); vkMod.shift = false; }
  else if (prefix) { wsSend(prefix + lower); }
  vkUpdateModUI();
}

// modifier toggle
$('vk-row-mod').querySelectorAll('.mod').forEach(b => {
  b.addEventListener('click', () => {
    vkMod[b.dataset.mod] = !vkMod[b.dataset.mod];
    vkUpdateModUI();
    log('VK', b.dataset.mod + '=' + vkMod[b.dataset.mod]);
  });
});

// letter / special / symbol keys
$('vk-panel').querySelectorAll('[data-key]').forEach(b => {
  if (b.classList.contains('mod')) return;
  b.addEventListener('click', () => {
    vkSend(b.dataset.key);
    // auto-dismiss sticky mod after one use
  });
});

// toggle keyboard
$('btn-kbd').addEventListener('click', () => {
  const vk = $('vk-panel');
  vk.style.display = vk.style.display === 'none' ? 'block' : 'none';
  log('VK', vk.style.display === 'block' ? 'shown' : 'hidden');
});

$('logout-btn').addEventListener('click', () => {
  if (ws) ws.close();
  localStorage.removeItem('ccmobile_token');
  token = '';
  mainScreen.style.display = 'none';
  loginScreen.style.display = 'flex';
  setStatus(false, 'offline');
});

let debugOn = false;
document.addEventListener('dblclick', function(e) {
  if (e.target.tagName === 'H1' || e.target.id === 'status-text') {
    debugOn = !debugOn;
    $('debug-panel').style.display = debugOn ? 'block' : 'none';
    if (debugOn) $('debug-panel').textContent = dbg.join('\n');
  }
});
log('INIT', navigator.userAgent.substr(0, 60));

function showMain() {
  loginScreen.style.display = 'none';
  mainScreen.style.display = 'flex';
  if (!term) initTerminal();
  startOverlay.style.display = 'flex';
  log('UI', 'main screen shown');
}

(async function init() {
  if (token && await checkToken()) { showMain(); return; }
  token = '';
  localStorage.removeItem('ccmobile_token');
  loginScreen.style.display = 'flex';
  log('UI', 'login screen');
})();
</script>
</body>
</html>"""


def main():
    if PASSWORD:
        print(f"[ccmobile] auth enabled, listening on :{PORT}")
    else:
        print(f"[ccmobile] WARNING: no password set — open access on :{PORT}")
    print(f"[ccmobile] workdir: {WORKDIR}")
    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda *_: None)


if __name__ == "__main__":
    main()
