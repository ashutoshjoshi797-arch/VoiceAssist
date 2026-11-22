"""HTTP plumbing shared by all providers (stdlib only, so it works on every CPU architecture).

* Remote (cloud) requests honour the system proxy settings.
* Local requests (Ollama, llama.cpp) bypass proxies and are refused unless the host is loopback,
  so the "local" path can never silently send data off the machine.
* Streams can be cancelled from another thread (used for barge-in / stop speaking).
* A process-wide socket guard can block every non-loopback connection (strict offline mode
  and the offline self-test).
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator


class NetworkError(Exception):
    """The network/host is unreachable (DNS failure, refused, timeout, connection dropped)."""


class HTTPStatusError(Exception):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {urllib.parse.urlsplit(url).netloc}: {body[:300]}")


class Cancelled(Exception):
    pass


class OfflineViolation(Exception):
    """Raised when something tries to reach the network while network access is forbidden."""


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if is_loopback_host(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_link_local
    except ValueError:
        return host.endswith(".local") or host.endswith(".lan")


# --------------------------------------------------------------------------- socket guard
_guard_lock = threading.Lock()
_guard_enabled = False
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo
violations: list[str] = []


def _addr_host(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return ""  # AF_UNIX etc.


def _guarded_connect(self, address):
    if _guard_enabled and self.family in (socket.AF_INET, socket.AF_INET6):
        host = _addr_host(address)
        if not is_loopback_host(host):
            violations.append(host)
            raise OfflineViolation(f"blocked non-loopback connection to {host} (offline mode)")
    return _orig_connect(self, address)


def _guarded_connect_ex(self, address):
    if _guard_enabled and self.family in (socket.AF_INET, socket.AF_INET6):
        host = _addr_host(address)
        if not is_loopback_host(host):
            violations.append(host)
            raise OfflineViolation(f"blocked non-loopback connection to {host} (offline mode)")
    return _orig_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if _guard_enabled and host is not None and not is_loopback_host(str(host)):
        violations.append(str(host))
        raise OfflineViolation(f"blocked DNS lookup of {host} (offline mode)")
    return _orig_getaddrinfo(host, *args, **kwargs)


def set_socket_guard(enabled: bool) -> None:
    """Block (True) or allow (False) every non-loopback connection in this process."""
    global _guard_enabled
    with _guard_lock:
        _guard_enabled = bool(enabled)
        socket.socket.connect = _guarded_connect  # type: ignore[assignment]
        socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
        socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]


def socket_guard_enabled() -> bool:
    return _guard_enabled


# --------------------------------------------------------------------------- requests
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SSL_CTX = ssl.create_default_context()


def _opener_for(url: str):
    host = urllib.parse.urlsplit(url).hostname or ""
    if is_private_host(host):
        return _NO_PROXY_OPENER
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))


def check_local_url(url: str, allow_lan: bool = False) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    ok = is_loopback_host(host) or (allow_lan and is_private_host(host))
    if not ok:
        raise OfflineViolation(
            f"Local engine URL {url!r} is not on this computer. Local mode only talks to "
            "localhost (enable 'allow LAN' in settings for a LAN server)."
        )


def _map_error(e: BaseException, url: str) -> Exception:
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return HTTPStatusError(e.code, body, url)
    if isinstance(e, OfflineViolation):
        return e
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        reason = getattr(e, "reason", e)
        if isinstance(reason, OfflineViolation):
            return reason
        return NetworkError(f"{urllib.parse.urlsplit(url).netloc}: {reason}")
    return e


class Stream:
    """An open HTTP response that can be iterated line by line and cancelled from any thread."""

    def __init__(self, resp, url: str, cancel_event: threading.Event | None):
        self._resp = resp
        self.url = url
        self.status = getattr(resp, "status", 200)
        self.headers = resp.headers
        self._cancel = cancel_event
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Shut the socket down so a blocking read in another thread returns immediately.
            sock = self._resp.fp.raw._sock  # type: ignore[attr-defined]
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._resp.close()
        except Exception:
            pass

    def iter_lines(self) -> Iterator[str]:
        try:
            while True:
                if self._cancel is not None and self._cancel.is_set():
                    raise Cancelled()
                line = self._resp.readline()
                if not line:
                    break
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        except Cancelled:
            raise
        except Exception as e:
            if (self._cancel is not None and self._cancel.is_set()) or self._closed:
                raise Cancelled() from e
            raise NetworkError(f"connection to {urllib.parse.urlsplit(self.url).netloc} dropped: {e}") from e
        finally:
            self.close()

    def iter_sse(self) -> Iterator[tuple[str, str]]:
        """Yield (event, data) pairs from a text/event-stream response."""
        event, data = "message", []
        for line in self.iter_lines():
            if line == "":
                if data:
                    yield event, "\n".join(data)
                event, data = "message", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            yield event, "\n".join(data)

    def read_all(self) -> bytes:
        try:
            return self._resp.read()
        except Exception as e:
            raise NetworkError(str(e)) from e
        finally:
            self.close()


def open_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body=None,
    data: bytes | None = None,
    timeout: float = 30.0,
    cancel_event: threading.Event | None = None,
    local_only: bool = False,
    allow_lan: bool = False,
) -> Stream:
    if local_only:
        check_local_url(url, allow_lan)
    host = urllib.parse.urlsplit(url).hostname or ""
    if _guard_enabled and not is_loopback_host(host):
        violations.append(host)
        raise OfflineViolation(f"blocked request to {host} (strict offline mode)")
    hdrs = {"User-Agent": "VoiceAssist/1.0", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = _opener_for(url).open(req, timeout=timeout)
    except BaseException as e:  # noqa: BLE001
        raise _map_error(e, url) from e
    stream = Stream(resp, url, cancel_event)
    if cancel_event is not None:
        # Wake up a blocked read when the turn is cancelled.
        def _watch():
            while not stream._closed:
                if cancel_event.wait(0.2):
                    stream.close()
                    return

        threading.Thread(target=_watch, daemon=True, name="stream-cancel-watch").start()
    return stream


def request_json(method: str, url: str, **kw):
    s = open_request(method, url, **kw)
    raw = s.read_all()
    return json.loads(raw.decode("utf-8") or "null")


def multipart_body(fields: dict, files: dict) -> tuple[bytes, str]:
    """files: name -> (filename, content_type, bytes)"""
    boundary = "----voiceassist" + ssl.RAND_bytes(8).hex()
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, ctype, content) in files.items():
        out += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
