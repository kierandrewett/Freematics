#!/usr/bin/env python3
"""Small management UI for the server-side Codex mechanic account."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

import mechanic_web


class CodexAccount:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: dict[int, queue.Queue] = {}
        self.next_id = 1
        self.login: dict | None = None
        self.login_result: dict | None = None
        self.process = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()
        self.call("initialize", {"clientInfo": {
            "name": "freematics_mechanic", "title": "Freematics Mechanic", "version": "1.0.0",
        }})
        self._send({"method": "initialized", "params": {}})

    def _send(self, message: dict) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("Codex account service stopped")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                if "id" in message:
                    waiting = self.pending.get(message["id"])
                    if waiting:
                        waiting.put(message)
                elif message.get("method") == "account/login/completed":
                    params = message.get("params") or {}
                    if self.login and params.get("loginId") == self.login.get("loginId"):
                        self.login_result = {"success": bool(params.get("success")),
                                             "error": params.get("error")}
                        self.login = None

    def call(self, method: str, params: dict | None = None) -> dict:
        waiting: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            self.pending[request_id] = waiting
            self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            message = waiting.get(timeout=15)
        except queue.Empty as error:
            raise RuntimeError("Codex account service timed out") from error
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        if "error" in message:
            raise RuntimeError(str(message["error"].get("message", "Codex request failed")))
        return message.get("result") or {}

    def status(self) -> dict:
        account = self.call("account/read", {"refreshToken": False}).get("account")
        with self.lock:
            return {"account": account, "login": self.login,
                    "login_result": self.login_result}

    def start_login(self) -> dict:
        with self.lock:
            if self.login:
                return self.login
        result = self.call("account/login/start", {"type": "chatgptDeviceCode"})
        if result.get("type") != "chatgptDeviceCode" or not result.get("loginId"):
            raise RuntimeError("Codex did not return a device sign-in code")
        with self.lock:
            self.login = {key: result[key] for key in ("loginId", "verificationUrl", "userCode")}
            self.login_result = None
            return self.login


ACCOUNT: CodexAccount | None = None
PUBLIC_ORIGIN = os.environ.get("FREEMATICS_UI_ORIGIN", "https://freematics.drewett.dev")
UI_ROOT = Path("/app/ui")


def page(state: dict, error: str = "") -> str:
    account = state.get("account") or {}
    logged_in = account.get("type") == "chatgpt"
    login = state.get("login")
    status = "Connected to ChatGPT" if logged_in else "Waiting for ChatGPT sign-in"
    if (state.get("login_result") or {}).get("error"):
        error = str(state["login_result"]["error"])
    detail = (f"{html.escape(str(account.get('email') or 'ChatGPT account'))} · "
              f"{html.escape(str(account.get('planType') or 'plan unknown'))}") if logged_in else ""
    login_block = ""
    if login and not logged_in:
        url = html.escape(str(login["verificationUrl"]), quote=True)
        code = html.escape(str(login["userCode"]))
        login_block = (f'<p>Open <a href="{url}" target="_blank" rel="noopener noreferrer">'
                       f'{url}</a> and enter this code:</p><strong class="code">{code}</strong>'
                       '<p>Return here after signing in. This page updates automatically.</p>')
    elif not logged_in:
        login_block = '<form method="post" action="/login"><button>Sign in to ChatGPT</button></form>'
    error_block = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="12"><title>Freematics mechanic</title>
<style>body{{font:16px system-ui;background:#101820;color:#f2f5f7;margin:0;padding:2rem}}
main{{max-width:680px;margin:auto}}section{{background:#1c2b35;border:1px solid #3b5260;
border-radius:14px;padding:1.5rem;margin:1rem 0}}a{{color:#8fd4ff}}button{{padding:.75rem 1rem;
font:inherit;border:0;border-radius:8px;cursor:pointer}}.code{{font-size:2rem;letter-spacing:.1em}}
.error{{color:#ffadad}}</style></head><body><main><h1>Freematics</h1>
<section><h2>Mechanic intelligence</h2><p>{status}</p><p>{detail}</p>{error_block}
{login_block}<p>Model: GPT-6 Sol. Reports use the authenticated telemetry MCP service.</p></section>
<section><h2>Vehicle data</h2><p><a href="https://freematics-admin.drewett.dev">Live collector</a></p>
<p>Trip reports appear after ChatGPT sign-in and the next analysis pass.</p></section>
</main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def json_response(self, status: int, value: dict) -> None:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def static_response(self, path: Path, content_type: str, cache: str) -> None:
        try:
            encoded = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path == "/":
            self.static_response(UI_ROOT / "index.html", "text/html; charset=utf-8", "no-store")
            return
        if path.startswith("/assets/") and re.fullmatch(r"/assets/[A-Za-z0-9_.-]+\.(?:css|js)", path):
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            self.static_response(UI_ROOT / path.lstrip("/"), content_type, "public, max-age=31536000, immutable")
            return
        if not path.startswith("/api/ui/"):
            self.send_error(404)
            return
        try:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            get = lambda key, default=None: query.get(key, [default])[0]
            if path == "/api/ui/status":
                result = mechanic_web.status()
                try:
                    assert ACCOUNT is not None
                    result["auth"] = ACCOUNT.status()
                except (RuntimeError, OSError) as error:
                    result["auth"] = {"account": None, "error": str(error)}
            elif path == "/api/ui/trips":
                result = mechanic_web.overview(get("before"), mechanic_web.bounded_int(get("limit"), 80, 1, 150))
            elif path == "/api/ui/trip":
                result = mechanic_web.detail(get("id"), mechanic_web.bounded_int(get("after"), -1, -1, 1000000000),
                                             mechanic_web.bounded_int(get("limit"), 12000, 1, 20000))
            elif path == "/api/ui/series":
                result = mechanic_web.series(get("trip"), get("pid"),
                                             mechanic_web.bounded_int(get("after"), -1, -1, 1000000000))
            elif path == "/api/ui/report":
                result = {"report": mechanic_web.report(get("trip"))}
            elif path == "/api/ui/sample":
                result = mechanic_web.sample(get("trip"), mechanic_web.bounded_int(get("seq"), 0, 0, 1000000000))
            else:
                self.send_error(404)
                return
            self.json_response(200, result)
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
        except LookupError as error:
            self.json_response(404, {"error": str(error)})
        except (OSError, sqlite3.Error, RuntimeError) as error:
            self.json_response(503, {"error": str(error)})

    def do_POST(self) -> None:
        if self.path != "/login" or self.headers.get("Origin") != PUBLIC_ORIGIN:
            self.send_error(403)
            return
        if self.headers.get("Content-Length") not in (None, "0"):
            self.send_error(413)
            return
        try:
            assert ACCOUNT is not None
            ACCOUNT.start_login()
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except (RuntimeError, OSError) as error:
            body = page({}, str(error)).encode()
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    ACCOUNT = CodexAccount()
    ThreadingHTTPServer(("0.0.0.0", 8020), Handler).serve_forever()
