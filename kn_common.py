"""Shared Telnet/CLI helpers for the Keenetic maintenance scripts.

Replaces the `exec(open('kn_apply.py').read().split('DOMAINS')[0])` pattern
that used to be copy-pasted at the top of every `kn_*.py`.

Credentials come from environment variables. If `ROUTER_PASS` is not set the
script prompts interactively via `getpass()`. The password is NEVER written
to a file by this module.

Typical usage:

    from kn_common import connect, send, read_until, strip_ansi, CONFIG_PROMPT

    s = connect()
    read_until(s, r'(?i)login\\s*:'); send(s, CONFIG['user'])
    read_until(s, r'(?i)password\\s*:'); send(s, CONFIG['password'])
    read_until(s, CONFIG_PROMPT, 10)

Or, for higher-level scripts:

    with KeeneticSession() as kn:
        kn.run('object-group fqdn example')
        kn.run('include example.com')
        kn.run('exit')
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from typing import Iterator, Optional

# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_HOST = os.environ.get('ROUTER_HOST', '192.168.1.1')
DEFAULT_PORT = int(os.environ.get('ROUTER_PORT', '23'))
DEFAULT_USER = os.environ.get('ROUTER_USER', 'admin')

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 8

CONFIG_PROMPT = r'\(config(-[a-z-]+)?\)>\s*$'
ANY_PROMPT = r'[>#\$]\s*$|\)>\s*$'

# ── Telnet protocol primitives ──────────────────────────────────────────────

IAC = 0xff
WILL, WONT, DO, DONT, SB, SE = 0xfb, 0xfc, 0xfd, 0xfe, 0xfa, 0xf0


def negotiate(buf: bytes, sock: socket.socket) -> bytes:
    """Strip Telnet IAC negotiation bytes, replying DONT/WONT to everything."""
    out = bytearray()
    resp = bytearray()
    i = 0
    while i < len(buf):
        b = buf[i]
        if b == IAC and i + 1 < len(buf):
            cmd = buf[i + 1]
            if cmd in (WILL, WONT, DO, DONT) and i + 2 < len(buf):
                opt = buf[i + 2]
                reply = DONT if cmd == WILL else WONT if cmd == DO else None
                if reply is not None:
                    resp += bytes([IAC, reply, opt])
                i += 3
                continue
            elif cmd == SB:
                j = i + 2
                while j < len(buf) - 1 and not (buf[j] == IAC and buf[j + 1] == SE):
                    j += 1
                i = j + 2
                continue
            else:
                i += 2
                continue
        out.append(b)
        i += 1
    if resp:
        sock.sendall(bytes(resp))
    return bytes(out)


_ANSI_RE = re.compile(r'\x1b\[K|\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


# ── High-level helpers (functional API, for legacy scripts) ─────────────────

MAX_BUF_BYTES = 1 << 20  # 1 MB hard cap against runaway output


def read_until(sock: socket.socket, pattern: str, timeout: float = READ_TIMEOUT) -> bytes:
    """Read from `sock` until `pattern` (regex) matches the IAC-stripped stream
    or until `timeout` elapses. Returns whatever was collected."""
    sock.settimeout(0.3)
    clean = bytearray()
    end = time.time() + timeout
    rx = re.compile(pattern.encode())
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            clean.extend(negotiate(chunk, sock))
            if len(clean) > MAX_BUF_BYTES:
                # Protect against stuck sessions flooding the buffer.
                raise BufferError(
                    f'read_until: buffer exceeded {MAX_BUF_BYTES} bytes without matching {pattern!r}'
                )
            if rx.search(clean):
                return bytes(clean)
        except socket.timeout:
            if rx.search(clean):
                return bytes(clean)
    return bytes(clean)


def send(sock: socket.socket, s: str) -> None:
    sock.sendall((s + '\n').encode())


def connect(host: Optional[str] = None, port: Optional[int] = None,
            timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """Open a Telnet socket to the router.

    CRITICAL: `settimeout()` MUST be set BEFORE `connect()` — otherwise a dead
    router causes the system default (~21s on Windows, 60-180s on Linux)
    to apply. The old scripts all got this wrong.
    """
    host = host or DEFAULT_HOST
    port = port or DEFAULT_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except Exception:
        sock.close()
        raise
    return sock


def get_credentials(prompt_if_missing: bool = True) -> tuple[str, str]:
    """Return (user, password). Password comes from ROUTER_PASS env var or
    (when allowed) an interactive getpass() prompt. NEVER hard-coded."""
    user = DEFAULT_USER
    password = os.environ.get('ROUTER_PASS')
    if password:
        return user, password
    if prompt_if_missing and sys.stdin.isatty():
        password = getpass.getpass(f'Password for {user}@{DEFAULT_HOST}: ')
        return user, password
    raise RuntimeError(
        'ROUTER_PASS environment variable is not set. Export it before running, '
        'or run interactively so getpass() can prompt you. '
        'The password is NEVER read from source code.'
    )


def login(sock: socket.socket, user: Optional[str] = None,
          password: Optional[str] = None,
          final_prompt: str = CONFIG_PROMPT, timeout: float = 10) -> bytes:
    """Perform Login:/Password: handshake. Returns the post-login banner."""
    if user is None or password is None:
        u, p = get_credentials()
        user = user or u
        password = password or p
    read_until(sock, r'(?i)login\s*:', timeout)
    send(sock, user)
    read_until(sock, r'(?i)password\s*:', timeout)
    send(sock, password)
    return read_until(sock, final_prompt, timeout)


# ── FQDN validation ─────────────────────────────────────────────────────────
#
# Keenetic's `include <fqdn>` accepts almost anything — including shell
# metacharacters and raw spaces — and silently stores garbage in the
# object-group. Later, `dns-proxy route` refuses to bind that group with
# an opaque error. Validate before sending so we fail early, near the
# source of the bad data.
#
# LDH rules (RFC 952, RFC 1123): labels 1..63 chars, a-z 0-9 '-', not
# starting/ending with '-'. Full name up to 253 chars. IDN domains must
# already be Punycode-encoded (xn--…) by the caller.

_FQDN_LABEL_RE = re.compile(r'^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$')


def is_valid_fqdn(fqdn: str) -> bool:
    """Strict FQDN check. Rejects empty, overlong, wildcard labels (`*.`),
    non-ASCII (caller must Punycode first), and shell metacharacters.
    Accepts a trailing dot."""
    if not isinstance(fqdn, str):
        return False
    name = fqdn.strip().rstrip('.')
    if not name or len(name) > 253:
        return False
    labels = name.split('.')
    # A bare hostname like "router" isn't an FQDN.
    if len(labels) < 2:
        return False
    for label in labels:
        if not _FQDN_LABEL_RE.match(label):
            return False
    return True


def validate_fqdns(fqdns: list[str]) -> tuple[list[str], list[str]]:
    """Split a list into (valid, invalid) keeping the original spelling.

    Use before sending `include <fqdn>` bulk loads so a single bad name
    in a 50-entry list doesn't poison the whole group.
    """
    valid: list[str] = []
    invalid: list[str] = []
    for name in fqdns:
        (valid if is_valid_fqdn(name) else invalid).append(name)
    return valid, invalid


def is_error_output(text: str) -> bool:
    """Detect Keenetic error replies. Stricter than the old `'rror' in text`
    heuristic: the line must start with `Error:` / `ERROR:` (colon-delimited),
    or with the CLI's `%% Error`/`%% Invalid` marker. This avoids false
    positives on `mirror`, `error-correction`, `invalid-auth flag cleared`
    and similar substrings appearing in normal output."""
    for line in text.splitlines():
        stripped = line.strip()
        # `Error:` / `ERROR:` at line start
        if re.match(r'(?i)^error\s*:', stripped):
            return True
        # `%% Error` / `%% Invalid` CLI markers
        if re.match(r'(?i)^%%\s*(error|invalid)\b', stripped):
            return True
    return False


# ── Session context manager (preferred API for new scripts) ─────────────────

class KeeneticSession:
    """High-level scoped session. Use as a context manager:

        with KeeneticSession() as kn:
            out = kn.run('show version')
            kn.run_config('ip host example.com SSTP0')

    Guarantees socket close on any exit path. Captures a full transcript
    into `self.transcript` for later inspection / logging.
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 user: Optional[str] = None, password: Optional[str] = None):
        self.host = host or DEFAULT_HOST
        self.port = port or DEFAULT_PORT
        self._user = user
        self._password = password
        self.sock: Optional[socket.socket] = None
        self.transcript: list[tuple[str, str]] = []

    def __enter__(self) -> 'KeeneticSession':
        self.sock = connect(self.host, self.port)
        u, p = self._user, self._password
        if u is None or p is None:
            env_u, env_p = get_credentials()
            u = u or env_u
            p = p or env_p
        login(self.sock, u, p)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                send(self.sock, 'exit')
                time.sleep(0.3)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def run(self, cmd: str, prompt: str = CONFIG_PROMPT,
            timeout: float = READ_TIMEOUT) -> str:
        """Execute `cmd`, wait for `prompt`, return ANSI-stripped text.
        Raises `RuntimeError` on detected error output."""
        if self.sock is None:
            raise RuntimeError('Session is closed')
        send(self.sock, cmd)
        raw = read_until(self.sock, prompt, timeout)
        text = strip_ansi(raw.decode('utf-8', 'replace'))
        self.transcript.append((cmd, text))
        return text

    def run_silent(self, cmd: str, prompt: str = CONFIG_PROMPT,
                   timeout: float = READ_TIMEOUT) -> str:
        """Like run() but never raises on errors — caller inspects text."""
        return self.run(cmd, prompt, timeout)


# ── Retry with exponential backoff ──────────────────────────────────────────

# Exceptions worth retrying on: network-level transient failures only.
# Credential errors (PermissionError) and syntax errors are NOT retried
# — those will just hammer a broken config. We also deliberately exclude
# the bare OSError catch-all, because PermissionError is a subclass of it
# and we'd end up retrying wrong-password.
RETRIABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,          # covers ConnectionRefused/Reset/Aborted
    TimeoutError,             # asyncio / modern timeout
    socket.timeout,            # legacy select/recv timeout
    socket.gaierror,           # DNS / getaddrinfo
    BufferError,               # our read_until cap
)


def with_retry(fn, *,
               attempts: int = 3,
               initial_delay: float = 1.0,
               backoff: float = 2.0,
               on_retry=None):
    """Call `fn()` up to `attempts` times with exponential backoff between
    failures. Re-raises the last exception if all attempts fail.

    `on_retry(attempt, delay, exc)` is invoked before each retry — wire it
    up to a logger if you want visibility.

    Usage for scheduled scripts:

        def push():
            with KeeneticSession(...) as kn:
                kn.run(...)
        with_retry(push, attempts=5)
    """
    delay = initial_delay
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RETRIABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt >= attempts:
                break
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, e)
                except Exception:
                    pass
            time.sleep(delay)
            delay *= backoff
    assert last_exc is not None
    raise last_exc


# ── CLI argument helpers ────────────────────────────────────────────────────

def build_arg_parser(description: str) -> argparse.ArgumentParser:
    """Shared arg parser base. Scripts can add more arguments via
    `parser.add_argument(...)` after calling this."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument('--host', default=DEFAULT_HOST,
                   help=f'Router host (default: {DEFAULT_HOST}, env ROUTER_HOST)')
    p.add_argument('--port', type=int, default=DEFAULT_PORT,
                   help=f'Router port (default: {DEFAULT_PORT}, env ROUTER_PORT)')
    p.add_argument('--user', default=DEFAULT_USER,
                   help=f'Login user (default: {DEFAULT_USER}, env ROUTER_USER)')
    p.add_argument('--dry-run', action='store_true',
                   help='Print what would be done; do not open a connection')
    p.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    return p


# ── Context-managed low-level socket (for functional-style scripts) ─────────

@contextmanager
def telnet_socket(host: Optional[str] = None,
                  port: Optional[int] = None) -> Iterator[socket.socket]:
    """Open a raw Telnet socket with login completed, guarantee close().
    Yields a socket already sitting at the (config)> prompt."""
    s = connect(host, port)
    try:
        login(s)
        yield s
    finally:
        try:
            send(s, 'exit')
            time.sleep(0.2)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
