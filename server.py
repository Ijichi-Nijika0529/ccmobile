#!/usr/bin/env python3
"""ccmobile — lightweight mobile remote control for Claude Code."""

import asyncio
import dataclasses
import hashlib
import errno
import fcntl
import json
import os
import pty
import re
import secrets
import struct
import termios
import time
from collections import deque
from pathlib import Path

from aiohttp import web

# ── config ──────────────────────────────────────────────────────────
PORT = int(os.environ.get("CCMOBILE_PORT", "8765"))
PASSWORD = os.environ.get("CCMOBILE_PASSWORD", "")
_WORKDIR = os.environ.get("CCMOBILE_WORKDIR", "")
WORKDIR = str(Path(_WORKDIR).resolve()) if _WORKDIR else str(Path.home())
TOKEN_EXPIRE = int(os.environ.get("CCMOBILE_TOKEN_EXPIRE", "86400"))
WS_MSG_MAX = 1_048_576       # 1MB max per WebSocket message
WS_IDLE_TIMEOUT = 1800        # 30min idle → close
LOGIN_RATE_LIMIT = 5          # max attempts per window
LOGIN_RATE_WINDOW = 60        # seconds
WS_PER_IP_LIMIT = 3           # max concurrent WS connections per IP
MAX_SESSIONS = 5              # max total active sessions
CLAUDE_SUDO_USER = os.environ.get("CCMOBILE_CLAUDE_USER", "")  # if set, spawn claude via sudo -u

_secret = secrets.token_hex(32)

# rate limiter: {ip: [(ts1, ts2, ...)]}
_rate_log: dict[str, list[float]] = {}
_rate_lock = asyncio.Lock()

# WS per-IP connection tracking: {ip: count}
_ws_per_ip: dict[str, int] = {}
_ws_per_ip_lock = asyncio.Lock()

# Register rate limiting: {ip: [timestamps]}
_register_rate_log: dict[str, list[float]] = {}
_register_rate_lock = asyncio.Lock()
REGISTER_RATE_LIMIT = 3
REGISTER_RATE_WINDOW = 3600  # 1 hour

# Password change rate limiting: {username: [timestamps]}
_password_change_rate_log: dict[str, list[float]] = {}
_password_change_rate_lock = asyncio.Lock()
PASSWORD_CHANGE_RATE_LIMIT = 5
PASSWORD_CHANGE_RATE_WINDOW = 3600


# ── accounts (multi-user support) ───────────────────────────────────
def _load_accounts() -> dict | None:
    """Load accounts.json if exists. Returns None for single-password mode."""
    # Try production path first, then local
    for path in ["/opt/ccmobile/accounts.json", "./accounts.json"]:
        if Path(path).exists():
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[ccmobile] Warning: failed to load {path}: {e}")
    return None


_accounts = _load_accounts()
if _accounts:
    print(f"[ccmobile] Multi-account mode: {len(_accounts)} accounts loaded")
else:
    print("[ccmobile] Single-password mode (no accounts.json found)")

_accounts_lock = asyncio.Lock()


async def _save_accounts(accounts: dict) -> None:
    """Save accounts to accounts.json with atomic write."""
    path = "/opt/ccmobile/accounts.json" if Path("/opt/ccmobile").exists() else "./accounts.json"
    async with _accounts_lock:
        # Write to temp file first
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(accounts, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Atomic rename
        os.replace(tmp_path, path)
        # Update global _accounts
        global _accounts
        _accounts = accounts
        print(f"[ccmobile] accounts.json updated, {len(accounts)} accounts")


async def _reload_accounts() -> dict | None:
    """Reload accounts from disk."""
    async with _accounts_lock:
        return _load_accounts()


# ── token helpers ────────────────────────────────────────────────────

def _verify_password(password: str, hash_str: str) -> bool:
    """Verify password against sha256:salt:hash format."""
    try:
        algo, salt, expected = hash_str.split(":", 2)
        if algo != "sha256":
            return False
        actual = hashlib.sha256((salt + password).encode()).hexdigest()
        return secrets.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


def _hash_token(username: str, ts: str) -> str:
    """Generate HMAC for token. username can be empty for single-password mode."""
    return hashlib.sha256(f"{PASSWORD}:{username}:{ts}:{_secret}".encode()).hexdigest()


async def _check_rate(ip: str) -> bool:
    """Returns True if within rate limit, False if exceeded."""
    now = time.time()
    async with _rate_lock:
        window = [t for t in _rate_log.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
        if len(window) >= LOGIN_RATE_LIMIT:
            return False
        window.append(now)
        _rate_log[ip] = window
        return True


async def _check_register_rate(ip: str) -> bool:
    """Check registration rate limit per IP."""
    now = time.time()
    async with _register_rate_lock:
        window = [t for t in _register_rate_log.get(ip, []) if now - t < REGISTER_RATE_WINDOW]
        if len(window) >= REGISTER_RATE_LIMIT:
            return False
        window.append(now)
        _register_rate_log[ip] = window
        return True


async def _check_password_change_rate(username: str) -> bool:
    """Check password change rate limit per user."""
    now = time.time()
    async with _password_change_rate_lock:
        window = [t for t in _password_change_rate_log.get(username, []) if now - t < PASSWORD_CHANGE_RATE_WINDOW]
        if len(window) >= PASSWORD_CHANGE_RATE_LIMIT:
            return False
        window.append(now)
        _password_change_rate_log[username] = window
        return True


def make_token(username: str = "") -> str:
    """Generate token. Format: username:timestamp:hmac"""
    ts = str(int(time.time()))
    return f"{username}:{ts}:{_hash_token(username, ts)}"


def check_token(token: str) -> str | None:
    """Validate token and return username (or empty string for single-password mode).
    Returns None if invalid."""
    if not token:
        return None
    try:
        parts = token.split(":", 3)
        if len(parts) < 3:
            return None
        username, ts_str, h = parts[0], parts[1], parts[2]
        if time.time() - int(ts_str) > TOKEN_EXPIRE:
            return None
        if not secrets.compare_digest(h, _hash_token(username, ts_str)):
            return None
        return username  # can be empty string
    except (ValueError, AttributeError, IndexError):
        return None


# ── session (one Claude process per working directory) ─────────────

@dataclasses.dataclass
class Session:
    workdir: str
    linux_user: str | None = None  # Linux user to run claude as (via sudo -u)
    pid: int | None = None
    fd: int | None = None
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    clients: "set[web.WebSocketResponse]" = dataclasses.field(default_factory=set)
    ring: "deque[bytes]" = dataclasses.field(default_factory=deque)
    ring_bytes: int = 0
    client_sizes: dict = dataclasses.field(default_factory=dict)
    broadcast_task: asyncio.Task | None = None
    name: str = ""          # short dir name for display/URL


_PTY_RING_SIZE = 10_485_760        # 10MB PTY history buffer (per session)

_sessions: dict[str, Session] = {}  # dirname → Session
_sessions_lock = asyncio.Lock()     # guards _sessions creation/deletion


def _safe(fn, *args):
    """Call fn, ignore OSError. Returns True if no exception."""
    try:
        fn(*args)
        return True
    except OSError:
        return False


async def _ws_error(request: web.Request, msg: str) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "error", "msg": msg}))
    await ws.close()
    return ws


# ── client registry ──────────────────────────────────────────────────

def _add_client(session: Session, ws: "web.WebSocketResponse") -> None:
    """Register a WebSocket client into a session."""
    session.clients.add(ws)


def _remove_client(session: Session, ws: "web.WebSocketResponse") -> None:
    """Unregister a WebSocket client. Does NOT kill Claude."""
    session.clients.discard(ws)


async def _ws_connect_allowed(ip: str) -> bool:
    """Check if this IP can open another WebSocket connection."""
    async with _ws_per_ip_lock:
        return _ws_per_ip.get(ip, 0) < WS_PER_IP_LIMIT


async def _ws_track_connect(ip: str) -> None:
    """Increment WS connection count for this IP."""
    async with _ws_per_ip_lock:
        _ws_per_ip[ip] = _ws_per_ip.get(ip, 0) + 1


async def _ws_track_disconnect(ip: str) -> None:
    """Decrement WS connection count for this IP."""
    async with _ws_per_ip_lock:
        count = _ws_per_ip.get(ip, 0)
        if count > 0:
            _ws_per_ip[ip] = count - 1
        if _ws_per_ip.get(ip, 0) == 0:
            _ws_per_ip.pop(ip, None)


# ── PTY write helper ─────────────────────────────────────────────────

async def _pty_write(session: Session, data: bytes) -> bool:
    """Write to the session's PTY fd.
    Handles BlockingIOError with add_writer retry.
    Returns False if the PTY slave has died or fd is invalid.
    """
    fd = session.fd
    if fd is None:
        return False

    loop = asyncio.get_running_loop()
    while True:
        try:
            os.write(fd, data)
            return True
        except BlockingIOError:
            fut = loop.create_future()
            def _on_writable():
                try:
                    loop.remove_writer(fd)
                except Exception:
                    pass
                if not fut.done():
                    fut.set_result(None)
            try:
                loop.add_writer(fd, _on_writable)
            except OSError:
                return False
            try:
                await asyncio.wait_for(fut, timeout=3.0)
            except asyncio.TimeoutError:
                return False
            except asyncio.CancelledError:
                return False
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                return False
            raise


# ── PTY window size (per session, multi-client takes max) ───────────

def _set_winsize(session: Session, cols: int, rows: int) -> None:
    """Apply terminal size to the session's PTY. Kernel sends SIGWINCH."""
    fd = session.fd
    if fd is None:
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


async def _apply_max_winsize(session: Session) -> None:
    """Set PTY to the max cols/rows across all clients of a session,
    then broadcast that size so every client renders the same grid.
    """
    if not session.client_sizes:
        return
    cols = max(c for c, r in session.client_sizes.values())
    rows = max(r for c, r in session.client_sizes.values())
    _set_winsize(session, cols, rows)
    msg = json.dumps({"type": "size", "cols": cols, "rows": rows})
    for w in list(session.clients):
        if not w.closed:
            try:
                await w.send_str(msg)
            except Exception:
                pass


# ── broadcast helpers ────────────────────────────────────────────────

async def _safe_send(ws: "web.WebSocketResponse", data: bytes) -> bool:
    """Send bytes to one WebSocket client. Returns True on success."""
    try:
        if ws.closed:
            return False
        await ws.send_bytes(data)
        await ws.drain()
        return True
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        return False


async def _cleanup_after_exit(session: Session) -> None:
    """Notify all session clients that Claude exited, close WS, reset state."""
    # Snapshot clients BEFORE marking Claude dead
    stale_clients = list(session.clients)

    # Mark Claude dead under lock
    async with session.lock:
        session.pid = None
        session.fd = None
        session.ring.clear()
        session.ring_bytes = 0
        session.client_sizes.clear()

    # Notify all stale clients
    exited_msg = json.dumps({"type": "exited"})
    for ws in stale_clients:
        try:
            if not ws.closed:
                await ws.send_str(exited_msg)
        except Exception:
            pass

    # Close all stale clients' WebSockets
    for ws in stale_clients:
        try:
            await ws.close()
        except Exception:
            pass
        session.clients.discard(ws)

    # Only clear broadcast_task if we are still the current task
    current = asyncio.current_task()
    if current is not None and session.broadcast_task is current:
        session.broadcast_task = None


async def _broadcast_pty(session: Session) -> None:
    """Read PTY → fan out to all session clients.
    Exits when Claude dies (EIO) or fd is closed (EBADF).
    On exit, calls _cleanup_after_exit.
    """
    fd = session.fd
    if fd is None:
        return

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                if e.errno == errno.EBADF:
                    break
                await asyncio.sleep(0.05)
                continue
            if not data:
                break

            # Append to ring buffer for new-client replay
            session.ring.append(data)
            session.ring_bytes += len(data)
            while session.ring_bytes > _PTY_RING_SIZE and session.ring:
                old = session.ring.popleft()
                session.ring_bytes -= len(old)

            # Fan out to all session clients
            clients = [w for w in list(session.clients) if not w.closed]
            if clients:
                results = await asyncio.gather(
                    *[_safe_send(w, data) for w in clients],
                    return_exceptions=True,
                )
                for w, ok in zip(clients, results):
                    if ok is not True:
                        session.clients.discard(w)
    finally:
        await _cleanup_after_exit(session)


# ── Claude lifecycle ─────────────────────────────────────────────────

async def spawn_claude(session: Session) -> int:
    Path(session.workdir).mkdir(parents=True, exist_ok=True)

    pid, fd = pty.fork()
    if pid == 0:
        # Child process
        # Clear sensitive environment variables
        for k in list(os.environ.keys()):
            if k not in ("PATH", "HOME", "TERM", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL"):
                os.environ.pop(k, None)

        os.chdir(session.workdir)

        # If session.linux_user is set, exec via sudo
        if session.linux_user:
            os.execvp("sudo", ["sudo", "-u", session.linux_user, "claude"])
        else:
            os.execvp("claude", ["claude"])
        os._exit(127)
    else:
        session.pid = pid
        session.fd = fd
        os.set_blocking(fd, False)
        session.broadcast_task = asyncio.create_task(_broadcast_pty(session))
        return fd


async def _ensure_claude(session: Session) -> None:
    """Ensure session's Claude is running. Spawns if not."""
    async with session.lock:
        if session.pid is not None:
            try:
                os.kill(session.pid, 0)
            except OSError:
                print(f"[ccmobile] {session.name}: PID existed but dead, cleaning up")
                session.pid = None
                session.fd = None
                session.broadcast_task = None
            else:
                return
        try:
            await spawn_claude(session)
            print(f"[ccmobile] {session.name}: Claude started (PID={session.pid})")
        except FileNotFoundError:
            raise RuntimeError("claude CLI not found")
        except Exception as e:
            raise RuntimeError(f"Failed to start Claude Code: {e}")


async def kill_claude(session: Session) -> None:
    """Kill the session's Claude process. Closing fd triggers cleanup chain."""
    async with session.lock:
        pid, fd = session.pid, session.fd
        if fd is not None:
            _safe(os.write, fd, b"\x04")
            await asyncio.sleep(0.5)
            _safe(os.close, fd)
            session.fd = None
        if pid is not None:
            _safe(os.kill, pid, 15)
            await asyncio.sleep(0.3)
            _safe(os.kill, pid, 9)
            # waitpid with proper error handling
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass  # already reaped
            session.pid = None


async def _get_or_create_session(dirname: str, linux_user: str | None, workdir_base: str) -> Session | None:
    """Look up or create a Session for the given directory name.
    Validates the path is within workdir_base. Returns None if invalid.

    Args:
        dirname: Directory name (relative to workdir_base)
        linux_user: Linux user to run claude as (None = no sudo)
        workdir_base: Base working directory for this user
    """
    # Security: only take the last component of dirname, discard any path separators
    # This prevents path traversal attacks like "../../etc"
    dirname_safe = Path(dirname).name if dirname != "." else "."

    # Construct and resolve the full path
    if dirname_safe == ".":
        workdir = workdir_base
    else:
        workdir = str((Path(workdir_base) / dirname_safe).resolve())

    # Verify the resolved path is within workdir_base (defense in depth)
    workdir_base_path = Path(workdir_base).resolve()
    workdir_target = Path(workdir).resolve()

    try:
        # Python 3.9+ has is_relative_to, fallback for 3.8
        if hasattr(workdir_target, 'is_relative_to'):
            if not workdir_target.is_relative_to(workdir_base_path):
                return None
        else:
            # Fallback: check commonpath
            if workdir_target != workdir_base_path:
                common = Path(os.path.commonpath([workdir_base_path, workdir_target]))
                if common != workdir_base_path:
                    return None
    except (ValueError, TypeError):
        return None

    # Use canonical path as key for dedup
    key = str(workdir_target)

    async with _sessions_lock:
        # Check global session limit
        if key not in _sessions and len(_sessions) >= MAX_SESSIONS:
            return None
        if key not in _sessions:
            _sessions[key] = Session(
                workdir=str(workdir_target),
                linux_user=linux_user,
                name=dirname_safe
            )
        return _sessions[key]


def _is_session_running(session: Session) -> bool:
    """Check if a session has a live Claude process."""
    if session.pid is None:
        return False
    try:
        os.kill(session.pid, 0)
        return True
    except OSError:
        return False


# ── HTTP handlers ────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")


def _get_token(request: web.Request) -> str:
    """Read token from cookie first, fallback to query param."""
    return request.cookies.get("ccmobile_token", "") or request.query.get("token", "")


def _check_origin(request: web.Request) -> bool:
    """Allow if no Origin header, or if Origin matches Host."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    try:
        origin_host = origin.split("://", 1)[1] if "://" in origin else origin
    except (IndexError, ValueError):
        return False
    return origin_host == host


async def handle_login(request: web.Request) -> web.Response:
    peer = request.remote or "?"

    # Rate limiting (applies to both modes)
    if (PASSWORD or _accounts) and not await _check_rate(peer):
        print(f"[login] {peer} rate limited")
        await asyncio.sleep(2)
        return web.json_response({"error": "too many attempts"}, status=429)

    # Detect if connection is via HTTPS (direct or behind proxy)
    is_https = (
        request.scheme == "https" or
        request.headers.get("X-Forwarded-Proto") == "https"
    )

    # Multi-account mode
    if _accounts:
        try:
            body = await request.json()
            username = body.get("username", "").strip()
            pw = body.get("password", "")
        except (json.JSONDecodeError, AttributeError):
            return web.json_response({"error": "bad request"}, status=400)

        if not username or username not in _accounts:
            await asyncio.sleep(1)
            return web.json_response({"error": "invalid username or password"}, status=403)

        account = _accounts[username]
        if not _verify_password(pw, account["password_hash"]):
            await asyncio.sleep(1)
            return web.json_response({"error": "invalid username or password"}, status=403)

        token = make_token(username)
        resp = web.json_response({"token": token, "expires": TOKEN_EXPIRE, "username": username})
        resp.set_cookie("ccmobile_token", token, max_age=TOKEN_EXPIRE,
                        httponly=True, samesite="Strict", secure=is_https)
        print(f"[login] {peer} logged in as {username}")
        return resp

    # Single-password mode (backward compatible)
    if not PASSWORD:
        token = make_token()
        resp = web.json_response({"token": token, "expires": TOKEN_EXPIRE})
    else:
        try:
            body = await request.json()
            pw = body.get("password", "")
        except (json.JSONDecodeError, AttributeError):
            return web.json_response({"error": "bad request"}, status=400)
        if not secrets.compare_digest(pw, PASSWORD):
            await asyncio.sleep(1)
            return web.json_response({"error": "wrong password"}, status=403)
        token = make_token()
        resp = web.json_response({"token": token, "expires": TOKEN_EXPIRE})
    resp.set_cookie("ccmobile_token", token, max_age=TOKEN_EXPIRE,
                    httponly=True, samesite="Strict", secure=is_https)
    return resp


async def handle_check(request: web.Request) -> web.Response:
    token = _get_token(request)
    username = check_token(token)
    if username is None:
        return web.json_response({"valid": False})
    return web.json_response({"valid": True, "username": username if username else None})


async def handle_auth_mode(request: web.Request) -> web.Response:
    """Return authentication mode: multi or single."""
    return web.json_response({"mode": "multi" if _accounts else "single"})


async def handle_register(request: web.Request) -> web.Response:
    """User self-registration. POST /api/register
    Body: {"username": "alice", "password": "...", "linux_user": "alice", "workdir": "/opt/workspace"}
    """
    peer = request.remote or "?"

    # Only allow registration in multi-account mode
    if not _accounts and _accounts is not None:
        return web.json_response({"error": "registration disabled in single-password mode"}, status=403)

    # Rate limiting (per IP, 3 per hour)
    if not await _check_register_rate(peer):
        print(f"[register] {peer} rate limited")
        await asyncio.sleep(2)
        return web.json_response({"error": "too many registrations"}, status=429)

    try:
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        linux_user = (body.get("linux_user") or username).strip()
        workdir = (body.get("workdir") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400)

    # Validate username
    if not username or len(username) < 3 or len(username) > 20:
        return web.json_response({"error": "username must be 3-20 characters"}, status=400)
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return web.json_response({"error": "username can only contain letters, numbers, underscore"}, status=400)

    # Validate password
    if len(password) < 8:
        return web.json_response({"error": "password must be at least 8 characters"}, status=400)

    # Check if username exists
    accounts = await _reload_accounts()
    if not accounts:
        accounts = {}
    if username in accounts:
        return web.json_response({"error": "username already exists"}, status=409)

    # Generate password hash
    salt = secrets.token_hex(8)
    hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
    password_hash = f"sha256:{salt}:{hash_value}"

    # Add to accounts
    accounts[username] = {
        "password_hash": password_hash,
        "linux_user": linux_user,
        "workdir": workdir if workdir else "/opt/workspace"
    }

    await _save_accounts(accounts)

    print(f"[register] {peer} registered user: {username}, linux_user: {linux_user}, workdir: {accounts[username]['workdir']}")
    return web.json_response({"ok": True, "username": username})


async def handle_change_password(request: web.Request) -> web.Response:
    """Change password for logged-in user. POST /api/change-password
    Body: {"old_password": "...", "new_password": "..."}
    """
    token = _get_token(request)
    username = check_token(token)
    if not username:
        return web.json_response({"error": "unauthorized"}, status=401)

    # Only allow in multi-account mode
    if not _accounts:
        return web.json_response({"error": "password change disabled in single-password mode"}, status=403)

    # Rate limiting (per user, 5 per hour)
    if not await _check_password_change_rate(username):
        print(f"[password] {username} rate limited")
        await asyncio.sleep(2)
        return web.json_response({"error": "too many password changes"}, status=429)

    try:
        body = await request.json()
        old_password = body.get("old_password", "")
        new_password = body.get("new_password", "")
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400)

    # Validate new password
    if len(new_password) < 8:
        return web.json_response({"error": "password must be at least 8 characters"}, status=400)

    # Verify old password
    accounts = await _reload_accounts()
    if not accounts or username not in accounts:
        return web.json_response({"error": "user not found"}, status=404)

    account = accounts[username]
    if not _verify_password(old_password, account["password_hash"]):
        await asyncio.sleep(1)
        return web.json_response({"error": "wrong old password"}, status=403)

    # Generate new password hash
    salt = secrets.token_hex(8)
    hash_value = hashlib.sha256((salt + new_password).encode()).hexdigest()
    password_hash = f"sha256:{salt}:{hash_value}"

    # Update accounts
    accounts[username]["password_hash"] = password_hash
    await _save_accounts(accounts)

    print(f"[password] {username} changed password")
    return web.json_response({"ok": True})


async def handle_dirs(request: web.Request) -> web.Response:
    """List workdir + direct subdirectories with running status.
    In multi-account mode, returns user's workdir. In single-password mode, returns WORKDIR."""
    token = _get_token(request)
    username = check_token(token)
    if username is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    # Determine base directory
    if _accounts:
        if not username:
            return web.json_response({"error": "username required in multi-account mode"}, status=400)
        account = _accounts.get(username)
        if not account:
            return web.json_response({"error": "unknown user"}, status=403)
        workdir_base = account.get("workdir") or f"/home/{account['linux_user']}/workspace"
    else:
        workdir_base = WORKDIR

    dirs = []
    # always include workdir_base itself as "."
    dirs.append({
        "name": ".",
        "label": Path(workdir_base).name or "workspace",
        "running": _is_session_running(_sessions.get(workdir_base, Session(workdir=workdir_base, name=".")))
                     if workdir_base in _sessions else False,
    })

    try:
        entries = sorted(os.scandir(workdir_base), key=lambda e: e.name.lower())
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            full = str(Path(entry.path).resolve())
            s = _sessions.get(full)
            dirs.append({
                "name": name,
                "label": name,
                "running": _is_session_running(s) if s else False,
            })
    except OSError:
        pass

    return web.json_response({"parent": workdir_base, "dirs": dirs})


async def handle_mkdir(request: web.Request) -> web.Response:
    """Create a new subdirectory under workdir_base (user-specific in multi-account mode)."""
    token = _get_token(request)
    username = check_token(token)
    if username is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    # Determine base directory
    if _accounts:
        if not username:
            return web.json_response({"error": "username required in multi-account mode"}, status=400)
        account = _accounts.get(username)
        if not account:
            return web.json_response({"error": "unknown user"}, status=403)
        workdir_base = account.get("workdir") or f"/home/{account['linux_user']}/workspace"
    else:
        workdir_base = WORKDIR

    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400)

    if not name or len(name) > 100:
        return web.json_response({"error": "invalid name"}, status=400)

    # reject dangerous characters
    for ch in ("..", "/", "\\", "\x00"):
        if ch in name:
            return web.json_response({"error": "invalid name"}, status=400)

    full = os.path.join(workdir_base, name)
    try:
        Path(full).mkdir(parents=False, exist_ok=True)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)

    print(f"[ccmobile] mkdir: {full}")
    return web.json_response({"ok": True, "path": full})


# ── WebSocket handler ────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    peer = request.remote or "?"

    # Check per-IP WS connection limit
    if not await _ws_connect_allowed(peer):
        print(f"[ws] {peer} connection limit exceeded ({WS_PER_IP_LIMIT} per IP)")
        return await _ws_error(request, f"Too many connections from your IP (max {WS_PER_IP_LIMIT})")

    if not _check_origin(request):
        print(f"[ws] {peer} bad origin: {request.headers.get('Origin', '?')}")
        return await _ws_error(request, "origin not allowed")

    token = _get_token(request)
    dirname = (request.query.get("dir") or ".").strip()

    # Validate token and extract username
    username = check_token(token)
    if username is None:
        print(f"[ws] {peer} bad token")
        return await _ws_error(request, "invalid token")

    # Determine linux_user and workdir_base based on mode
    if _accounts:
        if not username:
            print(f"[ws] {peer} multi-account mode requires username")
            return await _ws_error(request, "Multi-account mode requires username")
        account = _accounts.get(username)
        if not account:
            print(f"[ws] {peer} unknown user: {username}")
            return await _ws_error(request, f"Unknown user: {username}")
        linux_user = account["linux_user"]
        workdir_base = account.get("workdir") or f"/home/{linux_user}/workspace"
        print(f"[ws] {peer} user={username} dir={dirname} (multi-account mode)")
    else:
        # Single-password mode (backward compatible)
        linux_user = CLAUDE_SUDO_USER if CLAUDE_SUDO_USER else None
        workdir_base = WORKDIR
        print(f"[ws] {peer} dir={dirname} (single-password mode)")

    # Get or create session for this directory
    session = await _get_or_create_session(dirname, linux_user, workdir_base)
    if session is None:
        print(f"[ws] {peer} invalid dir or session limit: {dirname}")
        return await _ws_error(request, "Invalid directory or session limit reached")

    # Track this connection
    await _ws_track_connect(peer)

    # Stale lock detection for this session
    if session.lock.locked():
        print(f"[ws] {peer} stale lock for {session.name}, cleaning up...")
        await kill_claude(session)
        for i in range(10):
            if not session.lock.locked():
                break
            await asyncio.sleep(0.5)
        if session.lock.locked():
            print(f"[ws] {peer} {session.name} lock still held, forcing error")
            await _ws_track_disconnect(peer)
            return await _ws_error(request, "Please wait, session still closing")

    # Ensure Claude is running for this session
    try:
        await _ensure_claude(session)
    except RuntimeError as e:
        print(f"[ws] {peer} {session.name} Claude start failed: {e}")
        await _ws_track_disconnect(peer)
        return await _ws_error(request, str(e))

    ws = web.WebSocketResponse(heartbeat=45, compress=False)
    await ws.prepare(request)
    print(f"[ws] {peer} {session.name} PID={session.pid} fd={session.fd} user={session.linux_user or 'none'}")

    # Replay PTY history
    if session.ring:
        for chunk in list(session.ring):
            try:
                await ws.send_bytes(chunk)
            except Exception:
                break

    _add_client(session, ws)
    await ws.send_str(json.dumps({"type": "ready"}))

    msg_count = 0
    try:
        async for msg in ws:
            if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
            data = msg.data

            # JSON control messages
            if isinstance(data, str):
                ctrl = None
                try:
                    ctrl = json.loads(data)
                except (ValueError, TypeError):
                    ctrl = None
                if isinstance(ctrl, dict):
                    t = ctrl.get("type")
                    if t == "kill":
                        print(f"[ws] {peer} {session.name} requested kill")
                        await kill_claude(session)
                        break
                    if t == "resize":
                        cols = int(ctrl.get("cols") or 0)
                        rows = int(ctrl.get("rows") or 0)
                        if 0 < cols <= 1000 and 0 < rows <= 1000:
                            session.client_sizes[ws] = (cols, rows)
                            await _apply_max_winsize(session)
                        continue

            # Regular data → PTY
            if isinstance(data, str):
                data = data.encode()
            if not data:
                continue
            if len(data) > WS_MSG_MAX:
                continue

            msg_count += 1
            try:
                if not await _pty_write(session, data):
                    break
            except OSError:
                pass
    finally:
        print(f"[ws] {peer} {session.name} session end ({msg_count} msgs)")
        _remove_client(session, ws)
        session.client_sizes.pop(ws, None)
        await _apply_max_winsize(session)
        await _ws_track_disconnect(peer)
        try:
            await ws.close()
        except Exception:
            pass

    return ws


# ── app ─────────────────────────────────────────────────────────────

app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_post("/login", handle_login)
app.router.add_get("/check", handle_check)
app.router.add_get("/api/auth-mode", handle_auth_mode)
app.router.add_post("/api/register", handle_register)
app.router.add_post("/api/change-password", handle_change_password)
app.router.add_get("/api/dirs", handle_dirs)
app.router.add_post("/api/mkdir", handle_mkdir)
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
#login-screen input:focus,#input-row input:focus{border-color:var(--accent)}
#login-btn{padding:12px 28px;background:var(--accent);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;width:100%;max-width:320px}
#login-error{color:var(--danger);font-size:14px;min-height:20px}
#main-screen{display:none;flex-direction:column;height:100%}
#status-bar{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:var(--surface);border-bottom:1px solid var(--border);font-size:11px;flex-shrink:0}
#status-dot{width:8px;height:8px;border-radius:50%;background:var(--danger);flex-shrink:0}
#status-dot.on{background:var(--green)}
#status-left{display:flex;align-items:center;gap:6px;flex:1;min-width:0}
#status-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#logout-btn{font-size:10px;background:var(--border);color:var(--text);border:none;padding:4px 10px;border-radius:5px;cursor:pointer}
#terminal-container{flex:1;overflow:hidden;min-height:0;position:relative;user-select:text;-webkit-user-select:text}
#terminal-container .xterm{position:absolute;left:0;bottom:0;user-select:text;-webkit-user-select:text}
.xterm-viewport::-webkit-scrollbar{width:4px}
.xterm-viewport::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
#start-overlay{position:absolute;inset:0;z-index:10;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;background:var(--bg);gap:10px;padding:20px 16px;overflow-y:auto}
#start-overlay h2{font-size:16px;font-weight:600;margin-bottom:4px}
#dir-list{width:100%;max-width:360px;display:flex;flex-direction:column;gap:6px}
.dir-card{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--surface);border:2px solid var(--border);border-radius:10px;cursor:pointer;transition:border-color .15s;text-align:left}
.dir-card.selected{border-color:var(--accent)}
.dir-card.running{border-color:var(--green)}
.dir-card.running.selected{border-color:var(--accent)}
.dir-icon{font-size:22px;flex-shrink:0}
.dir-info{flex:1;min-width:0}
.dir-name{font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dir-status{font-size:11px;margin-top:2px}
.dir-status.stopped{color:var(--text);opacity:.5}
.dir-status.running{color:var(--green)}
.dir-status .dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dir-status .dot.running{background:var(--green)}
.dir-status .dot.stopped{background:var(--border)}
#dir-new{width:100%;max-width:360px;display:flex;gap:6px}
#dir-new-input{flex:1;padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;outline:none;min-width:0}
#dir-new-input:focus{border-color:var(--accent)}
#dir-new-btn{padding:10px 16px;background:var(--border);color:var(--text);border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;flex-shrink:0}
#start-btn{padding:14px 32px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;min-width:200px}
#start-btn:disabled{opacity:.5;cursor:default}
#toolbar{display:flex;gap:4px;padding:6px 8px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0;justify-content:center;flex-wrap:wrap}
.tb-btn{padding:8px 10px;font-size:12px;border-radius:6px;text-align:center;border:none;font-weight:600;cursor:pointer;color:#fff}
.tb-accent{background:var(--accent)}
.tb-danger{background:var(--danger)}
.tb-gray{background:var(--border);color:var(--text)}
.tb-enter{background:var(--green);flex:2;max-width:120px}
#input-row{display:flex;gap:4px;padding:4px 8px 8px;background:var(--surface);flex-shrink:0}
#input-row input{flex:1;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;outline:none;min-width:0}
#send-btn{padding:10px 16px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;flex-shrink:0}
#debug-panel{display:none;background:#000;color:var(--warn);font-size:10px;padding:4px 8px;max-height:80px;overflow-y:auto;flex-shrink:0;font-family:monospace;border-top:1px solid var(--border)}
#login-btn:active,#start-btn:active,.tb-btn:active,#send-btn:active,.vk-btn:active{opacity:.7}
/* virtual keyboard */
#vk-panel{display:none;flex-shrink:0;background:var(--surface);border-top:1px solid var(--border);padding:3px 4px;max-height:45vh;overflow-y:auto}
.vk-row{display:flex;gap:2px;justify-content:center;flex-wrap:wrap;margin:1px 0}
.vk-btn{min-width:26px;height:30px;padding:3px 5px;font-size:11px;border-radius:4px;border:none;font-weight:600;cursor:pointer;color:var(--text);background:var(--border);text-align:center;line-height:24px}
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
  <input id="username-input" type="text" placeholder="Username" enterkeyhint="next" autocomplete="username" style="display:none">
  <input id="pw-input" type="password" placeholder="Password" enterkeyhint="go" autocomplete="current-password">
  <button id="login-btn">Connect</button>
  <div style="margin-top:12px">
    <a href="#" id="register-link" style="color:var(--accent);font-size:13px;display:none">注册新账号</a>
  </div>
  <span id="login-error"></span>

  <!-- 注册表单 -->
  <div id="register-form" style="display:none">
    <h2>注册新账号</h2>
    <input id="reg-username" type="text" placeholder="Username (3-20字符)" enterkeyhint="next" autocomplete="off">
    <input id="reg-password" type="password" placeholder="Password (最少8字符)" enterkeyhint="next" autocomplete="new-password">
    <input id="reg-password2" type="password" placeholder="Confirm Password" enterkeyhint="go" autocomplete="new-password">
    <button id="register-btn">注册</button>
    <div style="margin-top:12px">
      <a href="#" id="back-login-link" style="color:var(--accent);font-size:13px">返回登录</a>
    </div>
    <span id="register-error"></span>
  </div>
</div>

<div id="main-screen">
  <div id="status-bar">
    <div id="status-left">
      <span id="status-dot"></span>
      <span id="status-text">offline</span>
    </div>
    <div>
      <button id="change-password-btn" style="display:none;margin-right:8px">修改密码</button>
      <button id="logout-btn">Logout</button>
    </div>
  </div>
  <div id="terminal-container">
    <div id="start-overlay">
      <h2>选择工作目录</h2>
      <div id="dir-list"></div>
      <div id="dir-new">
        <input id="dir-new-input" type="text" placeholder="新建目录..." enterkeyhint="done" autocomplete="off" autocapitalize="none">
        <button id="dir-new-btn">创建</button>
      </div>
      <button id="start-btn" disabled>请选择目录</button>
    </div>
  </div>
  <div id="toolbar">
    <button class="tb-btn tb-enter" id="btn-enter" data-key="enter">Enter &crarr;</button>
    <button class="tb-btn tb-accent" id="btn-c" data-key="ctrl-c">^C</button>
    <button class="tb-btn tb-accent" id="btn-d" data-key="ctrl-d">^D</button>
    <button class="tb-btn tb-gray" id="btn-left" data-key="left">&larr;</button>
    <button class="tb-btn tb-gray" id="btn-up" data-key="up">&uarr;</button>
    <button class="tb-btn tb-gray" id="btn-down" data-key="down">&darr;</button>
    <button class="tb-btn tb-gray" id="btn-right" data-key="right">&rarr;</button>
    <button class="tb-btn tb-gray" id="btn-tab" data-key="tab">Tab</button>
    <button class="tb-btn tb-gray" id="btn-esc" data-key="esc">Esc</button>
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
    <div class="vk-row" id="vk-row-q1"></div>
    <div class="vk-row" id="vk-row-q2"></div>
    <div class="vk-row" id="vk-row-q3"></div>
    <div class="vk-row" id="vk-row-sym">
      <button class="vk-btn sym" data-key="/">/</button><button class="vk-btn sym" data-key=".">.</button><button class="vk-btn sym" data-key="-">-</button><button class="vk-btn sym" data-key="_">_</button><button class="vk-btn sym" data-key="$">$</button><button class="vk-btn sym" data-key="#">#</button><button class="vk-btn sym" data-key="|">|</button><button class="vk-btn sym" data-key=">">></button><button class="vk-btn sym" data-key="~">~</button><button class="vk-btn sym" data-key="&">&amp;</button><button class="vk-btn sym" data-key=";">;</button><button class="vk-btn sym" data-key="*">*</button>
    </div>
  </div>
  <div id="input-row">
    <input id="msg-input" type="text" placeholder="Message..." enterkeyhint="send" autocomplete="off" autocapitalize="none">
    <button id="send-btn">Send</button>
  </div>
  <div id="debug-panel"></div>

  <!-- 修改密码模态框 -->
  <div id="change-password-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:9999;justify-content:center;align-items:center">
    <div style="background:var(--surface);padding:24px;border-radius:12px;width:90%;max-width:360px;box-shadow:0 4px 20px rgba(0,0,0,0.3)">
      <h3 style="margin:0 0 16px;color:var(--text)">修改密码</h3>
      <input id="old-password-input" type="password" placeholder="旧密码" style="width:100%;padding:10px;margin-bottom:12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
      <input id="new-password-input" type="password" placeholder="新密码 (最少8字符)" style="width:100%;padding:10px;margin-bottom:12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
      <input id="new-password2-input" type="password" placeholder="确认新密码" style="width:100%;padding:10px;margin-bottom:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
      <div style="display:flex;gap:8px">
        <button id="confirm-change-password-btn" style="flex:1;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer">确认</button>
        <button id="cancel-change-password-btn" style="flex:1;padding:10px;background:var(--border);color:var(--text);border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer">取消</button>
      </div>
      <span id="change-password-error" style="display:block;margin-top:12px;color:var(--danger);font-size:13px"></span>
    </div>
  </div>
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

let authenticated = false;
let ws = null, term = null, fit = null, byteCount = 0;
let authMode = 'single';  // 'single' or 'multi'
let currentUser = null;

const $ = id => document.getElementById(id);
const loginScreen = $('login-screen'), mainScreen = $('main-screen');
const usernameInput = $('username-input'), pwInput = $('pw-input'), loginBtn = $('login-btn'), loginError = $('login-error');
const registerLink = $('register-link'), registerForm = $('register-form'), backLoginLink = $('back-login-link');
const regUsername = $('reg-username'), regPassword = $('reg-password'), regPassword2 = $('reg-password2'), registerBtn = $('register-btn'), registerError = $('register-error');
const statusDot = $('status-dot'), statusText = $('status-text');
const changePasswordBtn = $('change-password-btn');
const changePasswordModal = $('change-password-modal'), oldPasswordInput = $('old-password-input'), newPasswordInput = $('new-password-input'), newPassword2Input = $('new-password2-input');
const confirmChangePasswordBtn = $('confirm-change-password-btn'), cancelChangePasswordBtn = $('cancel-change-password-btn'), changePasswordError = $('change-password-error');
const terminalContainer = $('terminal-container');
const startOverlay = $('start-overlay'), startBtn = $('start-btn');
const dirList = $('dir-list'), dirNewInput = $('dir-new-input'), dirNewBtn = $('dir-new-btn');
let selectedDir = null, dirsLoaded = false;
const msgInput = $('msg-input'), sendBtn = $('send-btn');

async function checkAuthMode() {
  try {
    const res = await fetch('/api/auth-mode');
    const data = await res.json();
    authMode = data.mode || 'single';
    log('AUTH', 'mode: ' + authMode);
    if (authMode === 'multi') {
      usernameInput.style.display = 'block';
      usernameInput.placeholder = 'Username';
      pwInput.setAttribute('enterkeyhint', 'go');
      usernameInput.addEventListener('keydown', e => { if (e.key === 'Enter') pwInput.focus(); });
      registerLink.style.display = 'inline';
    }
  } catch (e) {
    log('AUTH', 'checkAuthMode failed: ' + e.message);
  }
}

async function tryLogin(username, pw) {
  log('AUTH', 'logging in...');
  const body = authMode === 'multi'
    ? {username: username, password: pw}
    : {password: pw};
  const res = await fetch('/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (res.ok) {
    authenticated = true;
    currentUser = data.username || null;
    log('AUTH', 'OK, expires in ' + data.expires + 's' + (currentUser ? ', user=' + currentUser : ''));
    return true;
  }
  throw new Error(data.error || 'Login failed');
}

async function checkToken() {
  const res = await fetch('/check');
  const data = await res.json();
  log('AUTH', 'token check: ' + data.valid + (data.username ? ', user=' + data.username : ''));
  if (data.valid) {
    currentUser = data.username || null;
  }
  return data.valid;
}

async function doLogin() {
  loginError.textContent = '';
  try {
    const username = authMode === 'multi' ? usernameInput.value.trim() : '';
    const pw = pwInput.value;
    if (authMode === 'multi' && !username) {
      loginError.textContent = 'Username required';
      return;
    }
    if (await tryLogin(username, pw)) showMain();
  } catch (e) {
    loginError.textContent = e.message;
    log('AUTH', 'FAIL: ' + e.message);
  }
}

pwInput.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
loginBtn.addEventListener('click', doLogin);

// Register form
registerLink.addEventListener('click', e => {
  e.preventDefault();
  pwInput.value = '';
  usernameInput.value = '';
  loginError.textContent = '';
  loginScreen.querySelector('h1').style.display = 'none';
  usernameInput.style.display = 'none';
  pwInput.style.display = 'none';
  loginBtn.style.display = 'none';
  registerLink.parentElement.style.display = 'none';
  loginError.style.display = 'none';
  registerForm.style.display = 'block';
});

backLoginLink.addEventListener('click', e => {
  e.preventDefault();
  regUsername.value = '';
  regPassword.value = '';
  regPassword2.value = '';
  registerError.textContent = '';
  registerForm.style.display = 'none';
  loginScreen.querySelector('h1').style.display = 'block';
  if (authMode === 'multi') usernameInput.style.display = 'block';
  pwInput.style.display = 'block';
  loginBtn.style.display = 'block';
  registerLink.parentElement.style.display = 'block';
  loginError.style.display = 'block';
});

async function doRegister() {
  registerError.textContent = '';
  const username = regUsername.value.trim();
  const pw1 = regPassword.value;
  const pw2 = regPassword2.value;

  if (!username) {
    registerError.textContent = 'Username required';
    return;
  }
  if (pw1.length < 8) {
    registerError.textContent = 'Password must be at least 8 characters';
    return;
  }
  if (pw1 !== pw2) {
    registerError.textContent = 'Passwords do not match';
    return;
  }

  try {
    log('REGISTER', 'registering: ' + username);
    const res = await fetch('/api/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: username, password: pw1, linux_user: username})
    });
    const data = await res.json();
    if (res.ok) {
      log('REGISTER', 'success: ' + username);
      alert('注册成功！请使用新账号登录。\n\n注意：管理员需先创建 Linux 用户才能使用。');
      backLoginLink.click();
      usernameInput.value = username;
      pwInput.value = pw1;
    } else {
      registerError.textContent = data.error || 'Registration failed';
      log('REGISTER', 'FAIL: ' + (data.error || 'unknown'));
    }
  } catch (e) {
    registerError.textContent = e.message;
    log('REGISTER', 'FAIL: ' + e.message);
  }
}

regPassword2.addEventListener('keydown', e => { if (e.key === 'Enter') doRegister(); });
registerBtn.addEventListener('click', doRegister);

// Change password
changePasswordBtn.addEventListener('click', () => {
  oldPasswordInput.value = '';
  newPasswordInput.value = '';
  newPassword2Input.value = '';
  changePasswordError.textContent = '';
  changePasswordModal.style.display = 'flex';
});

cancelChangePasswordBtn.addEventListener('click', () => {
  changePasswordModal.style.display = 'none';
});

async function doChangePassword() {
  changePasswordError.textContent = '';
  const oldPw = oldPasswordInput.value;
  const newPw1 = newPasswordInput.value;
  const newPw2 = newPassword2Input.value;

  if (newPw1.length < 8) {
    changePasswordError.textContent = 'Password must be at least 8 characters';
    return;
  }
  if (newPw1 !== newPw2) {
    changePasswordError.textContent = 'Passwords do not match';
    return;
  }

  try {
    log('PASSWORD', 'changing password');
    const res = await fetch('/api/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({old_password: oldPw, new_password: newPw1})
    });
    const data = await res.json();
    if (res.ok) {
      log('PASSWORD', 'success');
      alert('密码修改成功！');
      changePasswordModal.style.display = 'none';
    } else {
      changePasswordError.textContent = data.error || 'Password change failed';
      log('PASSWORD', 'FAIL: ' + (data.error || 'unknown'));
    }
  } catch (e) {
    changePasswordError.textContent = e.message;
    log('PASSWORD', 'FAIL: ' + e.message);
  }
}

newPassword2Input.addEventListener('keydown', e => { if (e.key === 'Enter') doChangePassword(); });
confirmChangePasswordBtn.addEventListener('click', doChangePassword);

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
      scrollback: 5000,
      smoothScrollDuration: 0
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(terminalContainer);
    requestAnimationFrame(() => {
      try { fit.fit(); } catch(e){}
      requestAnimationFrame(() => { try { fit.fit(); } catch(e){} });
    });
    term.onData(data => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(data); });
    term.onBinary(data => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(data); });

    // ── clipboard: Ctrl+C copy + right-click copy ──
    term.attachCustomKeyEventHandler(e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'c' && term.hasSelection()) {
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        return false;  // prevent sending ^C to PTY when copying
      }
      // Ctrl+Shift+C always copies (like standard terminals)
      if ((e.ctrlKey && e.shiftKey) && e.key === 'C' && term.hasSelection()) {
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        return false;
      }
      return true;
    });
    terminalContainer.addEventListener('contextmenu', e => {
      if (term.hasSelection()) {
        e.preventDefault();
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        term.clearSelection();
      }
    });

// Batch wheel scroll in alternate screen to avoid per-tick WebSocket round-trip
(() => {
  const vp = terminalContainer.querySelector('.xterm-viewport');
  if (!vp) return;
  let acc = 0, lastSign = 0, flushT = null;
  const LH = 17, MAX = 15;

  function sendLines(n) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (n === 0) return;
    const cnt = Math.min(Math.abs(n), MAX);
    ws.send((n < 0 ? '\x1b[A' : '\x1b[B').repeat(cnt));
  }

  function flush() {
    if (flushT) { clearTimeout(flushT); flushT = null; }
    acc = 0; lastSign = 0;
  }

  function scheduleFlush() {
    if (flushT) clearTimeout(flushT);
    flushT = setTimeout(flush, 100);
  }

  vp.addEventListener('wheel', e => {
    if (term.buffer.active.type !== 'alternate') return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (e.ctrlKey || e.metaKey) return;
    e.preventDefault();

    const dy = e.deltaMode === 1 ? e.deltaY * LH : e.deltaY;
    if (dy === 0) return;

    const sign = Math.sign(dy);

    // direction change with meaningful residual: discard it
    if (lastSign !== 0 && sign !== 0 && sign !== lastSign && Math.abs(acc) >= LH * 0.5) {
      flush();
    }

    if (acc === 0 && Math.abs(dy) >= LH * 0.6) {
      // fast path: large first delta -> send immediately
      const lines = Math.trunc(dy / LH);
      sendLines(lines);
      acc = dy - lines * LH;
      lastSign = Math.sign(acc) || 0;
    } else {
      acc += dy;
      const lines = Math.trunc(acc / LH);
      if (lines !== 0) {
        sendLines(lines);
        acc -= lines * LH;
      }
      lastSign = Math.sign(acc) || 0;
    }

    scheduleFlush();
  }, { passive: false });

  // public reset for WS reconnect + tab hide
  window._resetScrollAcc = flush;

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) flush();
  });
})();

    let _rzT = null;
    window.addEventListener('resize', () => {
      clearTimeout(_rzT);
      _rzT = setTimeout(() => {
        try { fit.fit(); } catch(e){}
        if (ws && ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
      }, 150);
    });
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

function connectWS(dir) {
  return new Promise((resolve, reject) => {
    const dirParam = encodeURIComponent(dir || '.');
    const url = (location.protocol==='https:'?'wss':'ws') + '://' + location.host + '/ws?dir=' + dirParam;
    log('WS', 'connecting dir=' + (dir||'.') + '...');
    let sock;
    try { sock = new WebSocket(url); ws = sock; } catch(e) { log('WS', 'FAIL: '+e.message); reject(e); return; }
    sock.binaryType = 'arraybuffer';

    sock.onopen = () => {
      setStatus(true, 'connected');
      log('WS', 'open');
      if (window._resetScrollAcc) window._resetScrollAcc();
      sock._keepalive = setInterval(() => {
        if (sock && sock.readyState === WebSocket.OPEN) {
          sock.send('');
        }
      }, 10000);
    };

    sock.onmessage = e => {
      if (typeof e.data === 'string') {
        try {
          const m = JSON.parse(e.data);
          if (m.type === 'ready') {
            setStatus(true, 'Claude Code running');
            startOverlay.style.display = 'none';
            requestAnimationFrame(() => {
              try { fit.fit(); } catch(_){}
              if (ws && ws.readyState === WebSocket.OPEN)
                ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
            });
            log('WS', 'Claude Code READY');
            resolve();
          } else if (m.type === 'exited') {
            setStatus(false, 'exited');
            log('WS', 'Claude exited');
            term.clear();
            startOverlay.style.display = 'flex';
            loadDirs();
          } else if (m.type === 'size') {
            if (m.cols && m.rows && (term.cols !== m.cols || term.rows !== m.rows)) {
              try { term.resize(m.cols, m.rows); } catch(_){}
            }
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

    sock.onclose = () => {
      setStatus(false, 'disconnected');
      log('WS', 'closed after ' + byteCount + ' bytes received');
      if (sock._keepalive) {
        clearInterval(sock._keepalive);
        sock._keepalive = null;
      }
      if (ws === sock) {
        ws = null;
      }
    };

    sock.onerror = () => {
      log('WS', 'onerror');
      setStatus(false, 'error');
      reject(new Error('WebSocket error'));
    };
  });
}

async function loadDirs() {
  try {
    const res = await fetch('/api/dirs');
    const data = await res.json();
    dirList.innerHTML = '';
    selectedDir = null;
    data.dirs.forEach(d => {
      const card = document.createElement('div');
      card.className = 'dir-card' + (d.running ? ' running' : '');
      card.dataset.name = d.name;
      const icon = d.name === '.' ? '\u{1F3E0}' : '\u{1F4C1}';
      card.innerHTML =
        '<span class="dir-icon">' + icon + '</span>' +
        '<div class="dir-info">' +
          '<div class="dir-name">' + escapeHtml(d.label) + '</div>' +
          '<div class="dir-status ' + (d.running ? 'running' : 'stopped') + '">' +
            '<span class="dot ' + (d.running ? 'running' : 'stopped') + '"></span>' +
            (d.running ? '运行中' : '未启动') +
          '</div>' +
        '</div>';
      card.addEventListener('click', () => {
        dirList.querySelectorAll('.dir-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedDir = d.name;
        startBtn.disabled = false;
        startBtn.textContent = '进入 「' + d.label + '」';
      });
      dirList.appendChild(card);
    });
    dirsLoaded = true;
    startBtn.disabled = true;
    startBtn.textContent = '请选择目录';
    log('DIRS', data.dirs.length + ' dirs loaded');
  } catch (e) {
    log('DIRS', 'FAIL: ' + e.message);
  }
}

async function createDir() {
  const name = dirNewInput.value.trim();
  if (!name) return;
  dirNewBtn.disabled = true;
  dirNewBtn.textContent = '...';
  try {
    const res = await fetch('/api/mkdir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    const data = await res.json();
    if (res.ok) {
      log('MKDIR', 'ok: ' + name);
      dirNewInput.value = '';
      await loadDirs();
      // auto-select the newly created dir
      const card = dirList.querySelector('[data-name="' + CSS.escape(name) + '"]');
      if (card) card.click();
    } else {
      log('MKDIR', 'FAIL: ' + (data.error || 'unknown'));
      alert('创建失败: ' + (data.error || 'unknown'));
    }
  } catch (e) {
    log('MKDIR', 'FAIL: ' + e.message);
  }
  dirNewBtn.disabled = false;
  dirNewBtn.textContent = '创建';
}

// simple HTML escape
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function startClaude() {
  if (!selectedDir) return;
  byteCount = 0;
  startBtn.disabled = true;
  startBtn.textContent = 'Starting...';
  if (ws && ws._keepalive) {
    clearInterval(ws._keepalive);
    ws._keepalive = null;
  }
  try { await connectWS(selectedDir); } catch (e) {
    log('START', 'FAIL: ' + e.message);
    startBtn.disabled = false;
    startBtn.textContent = '请选择目录';
    await loadDirs();  // refresh running status
  }
}
startBtn.addEventListener('click', startClaude);
dirNewBtn.addEventListener('click', createDir);
dirNewInput.addEventListener('keydown', e => { if (e.key === 'Enter') createDir(); });

function wsReady() { return ws && ws.readyState === WebSocket.OPEN; }

function sendMsg() {
  const text = msgInput.value;
  if (!text) return;
  if (!wsReady()) { log('SEND', 'WS not open'); return; }
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
  if (wsReady()) {
    ws.send(data);
    log('SEND', '0x' + data.charCodeAt(0).toString(16));
  }
}
// toolbar buttons with data-key attribute
['btn-enter','btn-c','btn-d','btn-left','btn-up','btn-down','btn-right','btn-tab','btn-esc'].forEach(id => {
  const b = $(id);
  b.addEventListener('click', () => {
    const data = KEY_MAP[b.dataset.key];
    if (data !== undefined) wsSend(data);
  });
});
$('btn-kill').addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'kill'}));
    log('KILL', 'requested');
  }
});

// ── virtual keyboard ──
// generate QWERTY rows dynamically
const VK_ROWS = [['q','w','e','r','t','y','u','i','o','p'],['a','s','d','f','g','h','j','k','l'],['z','x','c','v','b','n','m']];
VK_ROWS.forEach((row, i) => {
  const el = $('vk-row-q' + (i + 1));
  row.forEach(k => { const b = document.createElement('button'); b.className = 'vk-btn'; b.dataset.key = k; b.textContent = k.toUpperCase(); el.appendChild(b); });
});

const vkMod = { ctrl: false, alt: false, shift: false, vt: false };
const KEY_MAP = {
  esc: '\x1b', tab: '\t', spc: ' ', bs: '\x7f', enter: '\r',
  home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~', del: '\x1b[3~',
  left: '\x1b[D', right: '\x1b[C', up: '\x1b[A', down: '\x1b[B',
  'ctrl-c': '\x03', 'ctrl-d': '\x04',
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
    // shift+tab: send immediately, don't wait for letter
    if (vkMod.shift && vkMod.vt) {
      wsSend('\x1b[Z');
      vkMod.shift = false;
      vkMod.vt = false;
      vkUpdateModUI();
      log('VK', 'shift+tab sent');
    }
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
  vk.style.display = vk.style.display === 'block' ? 'none' : 'block';
  log('VK', vk.style.display === 'block' ? 'shown' : 'hidden');
});

$('logout-btn').addEventListener('click', () => {
  if (ws) ws.close();
  authenticated = false;
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
  if (!dirsLoaded) loadDirs();
  // Update status text with username in multi-account mode
  if (currentUser) {
    statusText.textContent = 'User: ' + currentUser;
    changePasswordBtn.style.display = 'inline-block';
  }
  log('UI', 'main screen shown' + (currentUser ? ', user=' + currentUser : ''));
}

(async function init() {
  await checkAuthMode();
  if (await checkToken()) { authenticated = true; showMain(); return; }
  authenticated = false;
  loginScreen.style.display = 'flex';
  log('UI', 'login screen');
})();
</script>
</body>
</html>"""


def main():
    # Try to load self-signed SSL certificate if available
    ssl_context = None
    cert_path = Path("/opt/ccmobile/selfsigned.crt")
    key_path = Path("/opt/ccmobile/selfsigned.key")

    if cert_path.exists() and key_path.exists():
        try:
            import ssl
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))
            print(f"[ccmobile] HTTPS enabled (self-signed certificate)")
        except Exception as e:
            print(f"[ccmobile] WARNING: failed to load SSL cert: {e}")
            ssl_context = None

    if PASSWORD:
        scheme = "https" if ssl_context else "http"
        print(f"[ccmobile] auth enabled, listening on {scheme}://0.0.0.0:{PORT}")
    else:
        print(f"[ccmobile] WARNING: no password set — open access on :{PORT}")
    print(f"[ccmobile] workdir: {WORKDIR}")
    web.run_app(app, host="0.0.0.0", port=PORT, ssl_context=ssl_context, print=lambda *_: None)


if __name__ == "__main__":
    import sys
    # CLI tool: generate password hash
    if len(sys.argv) > 1 and sys.argv[1] == "hash-password":
        import getpass
        password = getpass.getpass("Enter password: ")
        salt = secrets.token_hex(8)
        hash_value = hashlib.sha256((salt + password).encode()).hexdigest()
        print(f"Salt: {salt}")
        print(f"Hash: sha256:{salt}:{hash_value}")
        print(f"\nAdd to accounts.json:")
        print(f'"password_hash": "sha256:{salt}:{hash_value}"')
        sys.exit(0)
    main()
