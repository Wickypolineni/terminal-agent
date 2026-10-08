"""http_request against a real local server."""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import check, done
from http_tool import HttpTool, _CheckedRedirects, classify


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/json"):
            self._send(200, json.dumps({"ok": True, "auth": self.headers.get("Authorization"),
                                        "query": self.path.partition("?")[2]}).encode())
        elif self.path == "/missing":
            self._send(404, b'{"error": "not found"}')
        elif self.path == "/redirect":
            self._send(302, b"", extra={"Location": f"http://127.0.0.1:{PORT}/json"})
        elif self.path == "/binary":
            self._send(200, b"\x00\x01\x02" * 100, ctype="application/octet-stream")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self._send(201, json.dumps({"received": json.loads(body)}).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{PORT}"
os.environ["TEST_TOKEN"] = "s3cr3t-value"
http = HttpTool({"local": {"env": "TEST_TOKEN", "hosts": ["127.0.0.1"]},
                 "unset": {"env": "NOT_SET_ANYWHERE", "hosts": ["127.0.0.1"]}})

out, err = http.request("GET", f"{base}/json", query={"a": 1})
check("GET returns status and pretty JSON", not err and out.startswith("HTTP 200") and '"ok": true' in out, out)
check("query parameters encoded", '"query": "a=1"' in out, out)
out, err = http.request("POST", f"{base}/post", json_body={"name": "agent"})
check("POST with a JSON body", not err and "HTTP 201" in out and '"name": "agent"' in out, out)
out, err = http.request("GET", f"{base}/missing")
check("404 comes back as an error with its body", err and "HTTP 404" in out and "not found" in out, out)
out, err = http.request("GET", f"{base}/binary")
check("binary body summarized, not dumped", "binary body" in out, out)

# credentials
out, err = http.request("GET", f"{base}/json", credential="local")
check("credential attached (server saw it)", not err and "Bearer [credential redacted]" in out, out)
check("credential value never appears in the result", "s3cr3t-value" not in out, out)
out, err = http.request("GET", f"http://localhost:{PORT}/json", credential="local")
check("credential refused for a host it isn't allowed for", err and "may only be sent to" in out, out)
check("unknown credential name", http.request("GET", f"{base}/json", credential="nope")[1])
check("credential whose env var is missing", "not set" in http.request("GET", f"{base}/json", credential="unset")[0])
out, err = http.request("GET", f"{base}/redirect", credential="local")
check("credential is NOT forwarded across a redirect", not err and '"auth": null' in out, out)

# address safety
check("127.0.0.1 classified private", classify(f"{base}/json")["private"])
check("public host classified public", not classify("https://example.com/")["private"]
      if classify("https://example.com/")["ok"] else True)   # no network: skip
out, err = http.request("GET", "http://169.254.169.254/latest/meta-data/")
check("cloud metadata address blocked", err and "metadata" in out, out)
out, err = http.request("GET", "file:///etc/passwd")
check("file:// URLs refused", err and "only http" in out, out)
req = urllib.request.Request("https://example.com/")
try:
    _CheckedRedirects(allow_private=False).redirect_request(req, None, 302, "Found", {}, f"{base}/json")
    blocked = False
except urllib.error.URLError:
    blocked = True
check("a public URL can't redirect into your local network", blocked)

server.shutdown()
done()
