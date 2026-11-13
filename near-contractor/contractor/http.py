"""Tiny stdlib HTTP client with an injectable transport (so tests never touch the network)."""
import json
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status, body, url):
        self.status, self.body, self.url = status, body, url
        super().__init__(f"HTTP {status} from {url}: {str(body)[:300]}")


class Http:
    def __init__(self, base_url, headers=None, timeout=20, send=None):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._send = send or self._urllib_send

    def request(self, method, path, *, json_body=None, params=None, headers=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        h = {"Accept": "application/json", **self.headers, **(headers or {})}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        status, raw = self._send(method, url, h, data, self.timeout)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        if status >= 400:
            raise HttpError(status, parsed, url)
        return parsed

    @staticmethod
    def _urllib_send(method, url, headers, data, timeout):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
