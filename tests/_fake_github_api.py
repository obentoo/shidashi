"""``FakeGitHubApi``: stands in for ``api.github.com`` in the story 032 tests.

A local ``http.server`` answers each ``(method, path)`` from :attr:`FakeGitHubApi.routes`
and records every request it receives. :meth:`FakeGitHubApi.install` points every HTTPS
connection ``urllib`` (or ``http.client``) opens at it, whatever host the request names, so
the code under test keeps its real ``https://api.github.com`` URLs and its own opener.
It also records the timeout each connection was asked for, and may cap it (``max_timeout``)
so a test of a 30 s timeout does not wait 30 s.

Tokens are minted at run time (:func:`mint_token`), never written in the source.
"""

import contextlib
import http.client
import http.server
import json
import secrets
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

JIT_PATH = "/repos/obentoo/shidashi/actions/runners/generate-jitconfig"
DISPATCH_PATH = "/repos/obentoo/shidashi/actions/workflows/weekly-release.yml/dispatches"
PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def mint_token() -> str:
    """A token-shaped secret made for one test."""
    return "test-token-" + secrets.token_hex(20)


def mint_jit_config() -> str:
    """An encoded JIT runner configuration made for one test (base64-looking)."""
    return secrets.token_urlsafe(96)


@dataclass(frozen=True)
class Reply:
    status: int
    body: object | None = None
    delay: float = 0.0
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def everything(self) -> str:
        """The request line, headers but ``Authorization``, and body, as one text."""
        shown = {k: v for k, v in self.headers.items() if k.lower() != "authorization"}
        return f"{self.method} {self.path}\n{shown}\n{self.body.decode('utf-8', 'replace')}"


@dataclass
class FakeGitHubApi:
    routes: dict[tuple[str, str], Reply] = field(default_factory=dict)
    requests: list[SeenRequest] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)
    port: int = 0
    _server: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                api.requests.append(
                    SeenRequest(self.command, path, {k: v for k, v in self.headers.items()}, body)
                )
                reply = api.routes.get((self.command, path), Reply(404, {"message": "Not Found"}))
                if reply.delay:
                    time.sleep(reply.delay)
                data = b"" if reply.body is None else json.dumps(reply.body).encode("utf-8")
                try:
                    self.send_response(reply.status)
                    for name, value in reply.headers:
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except BrokenPipeError, ConnectionResetError:
                    pass  # the client gave up (a timeout test)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        self._server = server
        self.port = server.server_address[1]
        # a short poll: stop() waits for one, after every test
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def install(self, monkeypatch: pytest.MonkeyPatch, *, max_timeout: float | None = None) -> None:
        """Every HTTPS connection goes to this server, in plain HTTP; no proxy is used."""
        for name in PROXY_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        api = self

        class ToFakeGitHubApi(http.client.HTTPConnection):
            def __init__(self, host: str, *args: Any, **kwargs: Any) -> None:
                del host, args
                kwargs.pop("context", None)
                kwargs.pop("check_hostname", None)
                asked = kwargs.pop("timeout", None)
                api.timeouts.append(asked)
                if max_timeout is not None and (
                    not isinstance(asked, (int, float)) or asked > max_timeout
                ):
                    asked = max_timeout
                if isinstance(asked, (int, float)):
                    kwargs["timeout"] = asked
                super().__init__("127.0.0.1", api.port, **kwargs)

        monkeypatch.setattr(http.client, "HTTPSConnection", ToFakeGitHubApi)

    def at(self, path: str) -> list[SeenRequest]:
        return [r for r in self.requests if r.path == path]


@contextlib.contextmanager
def serving_fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubApi]:
    """A started :class:`FakeGitHubApi`, installed for the block, stopped after it."""
    api = FakeGitHubApi()
    api.start()
    api.install(monkeypatch)
    try:
        yield api
    finally:
        api.stop()
