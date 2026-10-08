"""http_request: structured HTTP calls (GET/POST/PUT/PATCH/DELETE) without going through the shell.

Credentials are passed by reference: the model names one from config.json, and its value is
read from your environment at call time, attached only for that credential's allowed hosts,
never followed across redirects, and never shown to the model or written to the ledger.
Cloud metadata addresses are refused; private/loopback addresses need approval (policy.py).
"""
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from shell import OUTPUT_LIMIT

MAX_BODY_BYTES = 5_000_000
METADATA_HOSTS = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "metadata.google.internal"}
SHOWN_HEADERS = ("content-type", "content-length", "location", "retry-after", "www-authenticate",
                 "x-ratelimit-remaining")
TEXT_TYPES = ("text/", "json", "xml", "javascript", "html", "yaml", "csv", "x-www-form-urlencoded")


def classify(url: str) -> dict:
    """Where a URL points: {"ok", "error", "host", "private", "metadata"}."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"ok": False, "error": "only http:// and https:// URLs are supported", "host": "",
                "private": False, "metadata": False}
    host = parts.hostname.lower()
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return {"ok": False, "error": f"could not resolve {host}: {e}", "host": host,
                "private": False, "metadata": False}
    ips = {info[4][0].split("%")[0] for info in infos}
    metadata = host in METADATA_HOSTS or bool(ips & METADATA_HOSTS)
    private = any(not ipaddress.ip_address(ip).is_global for ip in ips)
    return {"ok": True, "error": "", "host": host, "private": private, "metadata": metadata}


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but re-check every hop against the same address rules."""
    max_redirections = 5

    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        facts = classify(newurl)
        if not facts["ok"] or facts["metadata"] or (facts["private"] and not self.allow_private):
            raise urllib.error.URLError(f"redirect to {newurl} was blocked "
                                        f"({facts['error'] or 'private or metadata address'})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpTool:
    def __init__(self, credentials: dict | None = None) -> None:
        self.credentials = credentials or {}

    def credential_names(self) -> list[str]:
        return sorted(self.credentials)

    def describe_credentials(self) -> str:
        if not self.credentials:
            return "none configured"
        return "; ".join(f"{name} (for {', '.join(cfg.get('hosts', []))})"
                         for name, cfg in sorted(self.credentials.items()))

    def request(self, method: str, url: str, headers: dict | None = None, query: dict | None = None,
                json_body=None, body: str | None = None, credential: str | None = None,
                timeout: int = 30):
        facts = classify(url)
        if not facts["ok"]:
            return facts["error"], True
        if facts["metadata"]:
            return "Requests to cloud metadata addresses are blocked.", True
        if query:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + sep + urllib.parse.urlencode({k: str(v) for k, v in query.items()})

        data = None
        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            data = body.encode()
        headers.setdefault("User-Agent", "sysadmin-agent/1.0")

        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        secret = None
        if credential:
            cfg = self.credentials.get(credential)
            if not cfg:
                return f"Unknown credential {credential!r}. Available: {', '.join(self.credential_names()) or 'none'}.", True
            if facts["host"] not in [h.lower() for h in cfg.get("hosts", [])]:
                return (f"Credential {credential!r} may only be sent to {', '.join(cfg.get('hosts', [])) or 'no hosts'}"
                        f" - not {facts['host']}."), True
            secret = os.environ.get(cfg.get("env", ""))
            if not secret:
                return f"Credential {credential!r}: environment variable {cfg.get('env')} is not set.", True
            # "unredirected": urllib never copies these headers onto a redirected request.
            req.add_unredirected_header(cfg.get("header", "Authorization"),
                                        cfg.get("format", "Bearer {value}").format(value=secret))

        opener = urllib.request.build_opener(_CheckedRedirects(allow_private=facts["private"]))
        try:
            with opener.open(req, timeout=timeout) as response:
                status, reason = response.status, response.reason
                resp_headers, raw = response.headers, response.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as e:
            status, reason, resp_headers = e.code, e.reason, e.headers
            raw = e.read(MAX_BODY_BYTES + 1) if e.fp else b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return f"Request failed: {getattr(e, 'reason', e)}", True

        text = self._format(status, reason, resp_headers, raw)
        if secret:
            text = text.replace(secret, "[credential redacted]")
        return text, status >= 400

    @staticmethod
    def _format(status, reason, headers, raw: bytes) -> str:
        lines = [f"HTTP {status} {reason}"]
        lines += [f"{name}: {headers[name]}" for name in SHOWN_HEADERS if headers.get(name)]
        content_type = (headers.get("content-type") or "").lower()
        cut = len(raw) > MAX_BODY_BYTES
        raw = raw[:MAX_BODY_BYTES]
        if raw and not any(t in content_type for t in TEXT_TYPES) and b"\0" in raw[:2048]:
            body = f"[binary body, {len(raw):,} bytes{'+' if cut else ''} - download it with curl via bash]"
        else:
            body = raw.decode(errors="replace")
            if "json" in content_type:
                try:
                    body = json.dumps(json.loads(body), indent=2)
                except ValueError:
                    pass
        if len(body) > OUTPUT_LIMIT:
            half = OUTPUT_LIMIT // 2
            body = f"{body[:half]}\n... [{len(body) - OUTPUT_LIMIT:,} characters omitted] ...\n{body[-half:]}"
        return "\n".join(lines) + "\n\n" + body
