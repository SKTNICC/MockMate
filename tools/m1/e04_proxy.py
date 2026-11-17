"""E04 真机时序证据代理：手机 -> 本机 8001 -> 后端 8000，逐请求写时间戳。

仅用于测试取证，不改变后端或生产配置。用法：
    python tools/m1/e04_proxy.py --listen 8001 --target http://127.0.0.1:8000 --log outputs/m1/e04-proxy.log
然后：adb reverse tcp:8000 tcp:8001，使手机 App 的 127.0.0.1:8000 经过本代理。
"""

from __future__ import annotations

import argparse
import http.server
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_PATH: Path
TARGET: str
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = TARGET + self.path
        request = urllib.request.Request(url, data=body, method=self.command)
        for name, value in self.headers.items():
            if name.lower() not in {"host", "content-length", "connection"}:
                request.add_header(name, value)
        started = time.time()
        code = 599
        data = b""
        headers = {}
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                code = response.status
                data = response.read()
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as error:
            code = error.code
            data = error.read()
            headers = dict(error.headers.items())
        except Exception as error:  # pragma: no cover - 代理自身故障要留痕
            with LOCK, LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')}.{int(started % 1 * 1000):03d} "
                    f"{self.command} {self.path} ERROR {type(error).__name__}\n"
                )
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with LOCK, LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')}.{int(started % 1 * 1000):03d} "
                f"{self.command} {self.path} {code}\n"
            )
        self.send_response(code)
        for name, value in headers.items():
            if name.lower() not in {"transfer-encoding", "connection", "content-length"}:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global LOG_PATH, TARGET
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=8001)
    parser.add_argument("--target", default="http://127.0.0.1:8000")
    parser.add_argument("--log", default="outputs/m1/e04-proxy.log")
    args = parser.parse_args()
    LOG_PATH = Path(args.log)
    TARGET = args.target.rstrip("/")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    server = ThreadingServer(("127.0.0.1", args.listen), Handler)
    print(f"proxy {args.listen} -> {TARGET}, log {LOG_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
