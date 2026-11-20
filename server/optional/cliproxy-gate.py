#!/usr/bin/python3 -I
"""cliproxy-gate: lets the CLIProxyAPI management panel through, and nothing else.

`tailscale serve` points at this small program (127.0.0.1:8318), not at the proxy itself. It forwards only the panel page
and the management API to the proxy on 127.0.0.1:8317. Every other path, including the model API (/v1/...), gets a 404 here,
so that part of the proxy is never reachable from the tailnet. The proxy still needs the management key for every
management call; this is a second fence, not a replacement.

Settings (environment): GATE_LISTEN (default 127.0.0.1:8318), GATE_UPSTREAM (default 127.0.0.1:8317).
"""
import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED_EXACT = ("/management.html", "/favicon.ico")
ALLOWED_PREFIX = ("/v0/management/", "/v8/management/")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host"}


def allowed(path):
    """True for the panel page and the management API, judged on the path alone (no query, no tricks)."""
    p = path.split("?", 1)[0].split("#", 1)[0]
    if "%" in p or ".." in p or "//" in p or "\\" in p:
        return False
    return p in ALLOWED_EXACT or p.startswith(ALLOWED_PREFIX)


def make_handler(upstream):
    host, _, port = upstream.rpartition(":")

    class Gate(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # one short line, never headers or query strings (they can hold keys)
            sys.stderr.write("gate %s %s\n" % (self.command, self.path.split("?", 1)[0]))

        def reply(self, code, text):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def handle_any(self):
            if not allowed(self.path):
                return self.reply(404, "not found\n")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10 * 1024 * 1024:
                return self.reply(413, "too large\n")
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            try:
                conn = http.client.HTTPConnection(host, int(port), timeout=60)
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException):
                return self.reply(502, "the proxy is not answering\n")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            conn.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

    return Gate


def make_server(listen, upstream):
    host, _, port = listen.rpartition(":")
    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("cliproxy-gate: it must listen on 127.0.0.1 only")
    return ThreadingHTTPServer((host, int(port)), make_handler(upstream))


if __name__ == "__main__":
    make_server(os.environ.get("GATE_LISTEN", "127.0.0.1:8318"), os.environ.get("GATE_UPSTREAM", "127.0.0.1:8317")).serve_forever()
