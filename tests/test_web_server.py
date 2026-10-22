import contextlib
import hmac
import json
import re
import secrets
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest
import uvicorn
from aihub_web.chat import ChatRequest
from aihub_web.config import Config, Limits
from aihub_web.db import Store
from aihub_web.providers import Finish, ProviderError, Text, Thinking, Usage
from aihub_web.security import CSP, SecurityMiddleware
from aihub_web.server import create_app, ndjson
from helpers import (
    FAST_PACING,
    HANG,
    HOLD,
    KEYS,
    TODAY,
    Clock,
    FakeEngine,
    FakeProvider,
    finished,
    generating,
    handler_for,
    make_service,
    serve_engine,
    utc_ms,
    webp,
)
from starlette.testclient import TestClient
from test_web_shutdown import free_port

BASE = "http://127.0.0.1:8787"
FREE = "openrouter/google/gemma-4-31b-it:free"
LOCAL = "ollama/qwen3:8b"
HAIKU = "anthropic/claude-haiku-5-5"
# Shaped like the desktop shell's token: 32 random bytes as 43 base64url characters.
TOKEN = secrets.token_urlsafe(32)


def build(tmp_path: Path, environ=KEYS, options: dict | None = None, **config):
    fakes = {
        "openrouter": FakeProvider(Text("Hello"), Finish("stop")),
        "ollama": FakeProvider(Text("Hi"), Finish("stop")),
        "anthropic": FakeProvider(Text("Hey"), Usage(1000, 500), Finish("end_turn")),
    }
    app = create_app(
        Config(state_dir=tmp_path, **config),
        environ=environ,
        transport=httpx2.MockTransport(handler_for()),
        providers=fakes,
        today=lambda: TODAY,
        image_pacing=FAST_PACING,
        **(options or {}),
    )
    return app, fakes


@pytest.fixture
def api(tmp_path: Path):
    app, fakes = build(tmp_path)
    with TestClient(app, base_url=BASE) as client:
        client.fakes = fakes
        yield client


def reply(client: TestClient, **body) -> list[dict]:
    with client.stream("POST", "/api/chat", json=body) as response:
        assert response.status_code == 200, response.read()
        return [json.loads(line) for line in response.iter_lines() if line]


class TestBootstrap:
    def test_everything_the_ui_needs_in_one_call(self, api: TestClient) -> None:
        data = api.get("/api/bootstrap").json()
        assert data["default_model"] == FREE
        assert data["retention_days"] == 30 and data["chats"] == []
        assert data["settings"] == {"instructions": ""}
        tiers = {m["tier"] for m in data["models"]}
        assert tiers == {"free", "local", "paid"}
        assert data["providers"]["ollama"]["ok"] is True

    def test_the_default_is_free_even_when_claude_is_listed_first_alphabetically(
        self, tmp_path: Path
    ) -> None:
        app, _ = build(tmp_path, environ={"AIHUB_ANTHROPIC_KEY": "k"})
        with TestClient(app, base_url=BASE) as client:
            assert client.get("/api/bootstrap").json()["default_model"] == LOCAL

    def test_models_endpoint(self, api: TestClient) -> None:
        data = api.get("/api/models?refresh=1").json()
        ids = {m["id"] for m in data["models"]}
        assert {FREE, LOCAL, HAIKU} <= ids

    def test_health(self, api: TestClient) -> None:
        assert api.get("/api/health").json() == {"ok": True}


class TestChatFlow:
    def test_stream_then_history(self, api: TestClient) -> None:
        api.fakes["openrouter"].script = [
            Thinking("hm"),
            Text("Hel"),
            Text("lo"),
            Finish("stop"),
        ]
        with api.stream(
            "POST", "/api/chat", json={"text": "Hi there", "model": FREE}
        ) as response:
            assert response.headers["content-type"].startswith("application/x-ndjson")
            assert response.headers["cache-control"] == "no-cache, no-transform"
            events = [json.loads(line) for line in response.iter_lines() if line]
        assert [e["t"] for e in events] == ["chat", "think", "text", "text", "done"]

        chat_id = events[0]["chat"]["id"]
        listed = api.get("/api/chats").json()["chats"]
        assert [c["id"] for c in listed] == [chat_id]
        detail = api.get(f"/api/chats/{chat_id}").json()
        assert [(m["role"], m["content"]) for m in detail["messages"]] == [
            ("user", "Hi there"),
            ("assistant", "Hello"),
        ]
        assert detail["messages"][1]["thinking"] == "hm"

    def test_paid_replies_carry_their_cost(self, api: TestClient) -> None:
        events = reply(api, text="q", model=HAIKU, effort="off")
        assert events[-1]["message"]["usage"]["cost"] == "0.00035"
        assert api.fakes["anthropic"].requests[-1].effort == "off"

    def test_a_provider_error_arrives_as_an_event_with_the_partial_reply(
        self, api: TestClient
    ) -> None:
        api.fakes["openrouter"].script = [
            Text("half"),
            ProviderError("Free limit reached", "rate_limit"),
        ]
        events = reply(api, text="q", model=FREE)
        assert events[-1]["t"] == "error"
        assert events[-1]["message"]["error"]["code"] == "rate_limit"
        assert events[-1]["message"]["content"] == "half"

    def test_regenerate(self, api: TestClient) -> None:
        started = reply(api, text="q", model=FREE)[0]
        chat_id = started["chat"]["id"]
        api.fakes["openrouter"].script = [Text("Again"), Finish("stop")]
        reply(api, chat_id=chat_id, regenerate_for=started["user"]["id"])
        messages = api.get(f"/api/chats/{chat_id}").json()["messages"]
        assert [m["content"] for m in messages] == ["q", "Again"]

    def test_retrying_an_old_exchange_from_a_stale_window_is_a_conflict(
        self, api: TestClient
    ) -> None:
        started = reply(api, text="one", model=FREE)[0]
        chat_id = started["chat"]["id"]
        reply(api, chat_id=chat_id, text="two")
        response = api.post(
            "/api/chat",
            json={"chat_id": chat_id, "regenerate_for": started["user"]["id"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_chat"
        messages = api.get(f"/api/chats/{chat_id}").json()["messages"]
        assert [m["role"] for m in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

    def test_standing_instructions_apply_to_the_next_reply(
        self, api: TestClient
    ) -> None:
        assert api.put(
            "/api/settings", json={"instructions": "  Be terse.  "}
        ).json() == {"instructions": "Be terse."}
        reply(api, text="q", model=FREE)
        assert "Be terse." in api.fakes["openrouter"].requests[-1].system

    def test_search(self, api: TestClient) -> None:
        reply(api, text="remember the zebra crossing", model=FREE)
        reply(api, text="something else entirely", model=FREE)
        found = api.get("/api/chats", params={"q": "zebra"}).json()["chats"]
        assert len(found) == 1 and "zebra" in found[0]["snippet"]

    def test_rename_and_delete(self, api: TestClient) -> None:
        chat_id = reply(api, text="q", model=FREE)[0]["chat"]["id"]
        renamed = api.patch(
            f"/api/chats/{chat_id}", json={"title": "  A   better\ttitle "}
        )
        assert renamed.json()["title"] == "A better title"
        assert api.delete(f"/api/chats/{chat_id}").status_code == 204
        assert api.get(f"/api/chats/{chat_id}").status_code == 404
        assert api.delete(f"/api/chats/{chat_id}").status_code == 404

    def test_deleting_everything_needs_the_confirmation_word(
        self, api: TestClient
    ) -> None:
        reply(api, text="a", model=FREE)
        reply(api, text="b", model=FREE)
        assert api.request("DELETE", "/api/chats", json={}).status_code == 422
        assert (
            api.request("DELETE", "/api/chats", json={"confirm": "yes"}).status_code
            == 422
        )
        assert len(api.get("/api/chats").json()["chats"]) == 2
        assert api.request(
            "DELETE", "/api/chats", json={"confirm": "delete-all"}
        ).json() == {"deleted": 2}
        assert api.get("/api/chats").json()["chats"] == []


class TestRefusals:
    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"text": ""}, 422, "invalid"),
            ({"text": "hi", "bogus": 1}, 422, "invalid"),
            ({"text": "hi", "model": "ollama/nope:1b"}, 400, "unknown_model"),
            ({"text": "hi", "chat_id": "0123456789abcdef"}, 404, "no_chat"),
            ({"text": "hi", "chat_id": "../../etc/passwd"}, 422, "invalid"),
            ({"regenerate_for": "0123456789abcdef"}, 422, "invalid"),
        ],
    )
    def test_bad_requests_get_a_json_error_before_any_streaming(
        self, api: TestClient, body: dict, status: int, code: str
    ) -> None:
        response = api.post("/api/chat", json=body)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert api.get("/api/chats").json()["chats"] == []

    def test_malformed_json(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat", content=b"{nope", headers={"content-type": "application/json"}
        )
        assert (response.status_code, response.json()["error"]["code"]) == (
            400,
            "bad_json",
        )

    def test_model_without_a_key(self, tmp_path: Path) -> None:
        app, _ = build(tmp_path, environ={})
        with TestClient(app, base_url=BASE) as client:
            response = client.post("/api/chat", json={"text": "hi", "model": HAIKU})
            assert response.status_code == 409
            assert "AIHUB_ANTHROPIC_KEY" in response.json()["error"]["message"]

    def test_unknown_endpoints_and_chat_ids(self, api: TestClient) -> None:
        assert api.get("/api/nope").json()["error"]["code"] == "not_found"
        assert api.get("/api/chats/not-an-id").status_code == 404

    def test_settings_are_bounded(self, api: TestClient) -> None:
        assert (
            api.put("/api/settings", json={"instructions": "x" * 4001}).status_code
            == 422
        )


class TestGuards:
    def test_foreign_hosts_are_refused_to_stop_dns_rebinding(
        self, api: TestClient
    ) -> None:
        refused = api.get("/api/bootstrap", headers={"host": "evil.example:8787"})
        assert refused.status_code == 400 and "allowed_hosts" in refused.text
        assert (
            api.get("/api/health", headers={"host": "localhost:8787"}).status_code
            == 200
        )

    @pytest.mark.parametrize(
        "origin", ["https://evil.example", "null", "http://127.0.0.1.evil.example"]
    )
    def test_cross_site_writes_are_refused(self, api: TestClient, origin: str) -> None:
        response = api.post(
            "/api/chat", json={"text": "hi"}, headers={"origin": origin}
        )
        assert response.status_code == 403
        assert api.get("/api/chats").json()["chats"] == []

    def test_same_origin_writes_pass(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat",
            json={"text": "hi", "model": FREE},
            headers={"origin": BASE, "sec-fetch-site": "same-origin"},
        )
        assert response.status_code == 200

    def test_fetch_metadata_cross_site_is_refused(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat", json={"text": "hi"}, headers={"sec-fetch-site": "cross-site"}
        )
        assert response.status_code == 403

    def test_a_write_must_be_json(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat", content='{"text":"hi"}', headers={"content-type": "text/plain"}
        )
        assert response.status_code == 415

    def test_oversized_bodies_are_cut_off(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat",
            content=b"x" * (1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413

    def test_a_tailnet_name_works_once_it_is_allowed(self, tmp_path: Path) -> None:
        app, _ = build(tmp_path, allowed_hosts=("127.0.0.1", "box.tail1234.ts.net"))
        with TestClient(app, base_url="https://box.tail1234.ts.net") as client:
            assert client.get("/api/health").status_code == 200
            posted = client.post(
                "/api/chat",
                json={"text": "hi"},
                headers={"origin": "https://box.tail1234.ts.net"},
            )
            assert posted.status_code == 200

    def test_headers_on_api_and_pages(self, api: TestClient) -> None:
        for path in ("/api/health", "/"):
            headers = api.get(path).headers
            assert headers["content-security-policy"] == CSP
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["referrer-policy"] == "no-referrer"
            assert "access-control-allow-origin" not in headers
        assert api.get("/api/health").headers["cache-control"] == "no-store"
        assert api.get("/").headers["cache-control"] == "no-cache"

    def test_the_policy_blocks_inline_script_and_remote_content(self) -> None:
        assert (
            "script-src 'self'" in CSP
            and "unsafe-inline" not in CSP
            and "unsafe-eval" not in CSP
        )
        assert "img-src 'self'" in CSP and "connect-src 'self'" in CSP
        assert "frame-ancestors 'none'" in CSP and "default-src 'none'" in CSP

    @pytest.mark.parametrize(
        "path",
        [
            "/..%2f..%2f..%2fetc/passwd",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/lib/../../../etc/passwd",
        ],
    )
    def test_static_files_cannot_escape(self, api: TestClient, path: str) -> None:
        assert api.get(path).status_code in (404, 400)


def cookie(value: str | bytes) -> dict[str, bytes]:
    """A Cookie header carrying `value` as the launch token; bytes lets a test send what isn't text."""
    raw = value.encode() if isinstance(value, str) else value
    return {"cookie": b"aihub_t=" + raw}


@pytest.fixture
def guarded(tmp_path: Path):
    """The app as the desktop shell starts it: a per-launch token guards /api."""
    app, fakes = build(tmp_path, environ={**KEYS, "AIHUB_LAUNCH_TOKEN": TOKEN})
    with TestClient(app, base_url=BASE) as client:
        client.fakes = fakes
        yield client


class TestLaunchToken:
    @pytest.mark.parametrize("path", ["/api/bootstrap", "/api/health"])
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="missing"),
            pytest.param(cookie("wrong"), id="wrong"),
            pytest.param(cookie(""), id="empty"),
            pytest.param(cookie(TOKEN + "x"), id="too long"),
            pytest.param(cookie(TOKEN[:-1]), id="too short"),
            pytest.param({"cookie": f"other={TOKEN}"}, id="under another name"),
            pytest.param(cookie("t\u00f6ken".encode()), id="non-ascii utf-8"),
            pytest.param(cookie(b"\xff\xfe"), id="not utf-8"),
        ],
    )
    def test_a_request_without_the_token_is_refused(
        self, guarded: TestClient, path: str, headers: dict
    ) -> None:
        response = guarded.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {
            "error": {"code": "not_authorised", "message": "Not authorised."}
        }
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == CSP

    @pytest.mark.parametrize("path", ["/api/bootstrap", "/api/health"])
    def test_the_right_cookie_gets_through(
        self, guarded: TestClient, path: str
    ) -> None:
        assert guarded.get(path, headers=cookie(TOKEN)).status_code == 200

    def test_the_token_may_sit_among_other_cookies(self, guarded: TestClient) -> None:
        headers = {"cookie": f"theme=dark; aihub_t={TOKEN}; sidebar=open"}
        assert guarded.get("/api/health", headers=headers).status_code == 200

    def test_a_value_wider_than_latin_1_is_refused_not_a_server_error(
        self, guarded: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Starlette's cookie parser yields nothing wider than latin-1 today. If it ever did, the
        comparison must stay a mismatch (401) and not become an encoding error (500)."""
        monkeypatch.setattr(
            "aihub_web.security.cookie_parser", lambda _: {"aihub_t": "t€ken"}
        )
        assert guarded.get("/api/health", headers=cookie(TOKEN)).status_code == 401

    def test_every_api_path_and_method_needs_it(self, guarded: TestClient) -> None:
        for method, path in [
            ("POST", "/api/chat"),
            ("GET", "/api/chats"),
            ("PUT", "/api/settings"),
            ("DELETE", "/api/chats"),
            ("PATCH", "/api/settings"),
            ("HEAD", "/api/health"),
            ("OPTIONS", "/api/chats"),
            ("GET", "/api/nope"),
            ("GET", "/api/"),
        ]:
            assert guarded.request(method, path).status_code == 401, (method, path)
        assert guarded.get("/api/nope", headers=cookie(TOKEN)).status_code == 404
        assert guarded.get("/api/chats", headers=cookie(TOKEN)).json() == {"chats": []}

    def test_the_token_is_compared_with_compare_digest_on_bytes(
        self, guarded: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A test can't observe timing, so pin the call that makes the comparison constant-time."""
        seen = []
        real = hmac.compare_digest

        def spy(a, b):
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(hmac, "compare_digest", spy)
        guarded.get("/api/health", headers=cookie(TOKEN))
        assert seen == [(TOKEN.encode(), TOKEN.encode())]

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/index.html",
            "/js/main.js",
            "/css/app.css",
            "/manifest.webmanifest",
            "/icon.svg",
        ],
    )
    def test_the_page_and_its_files_stay_public(
        self, guarded: TestClient, path: str
    ) -> None:
        assert guarded.get(path).status_code == 200

    @pytest.mark.parametrize("path", ["/api", "/apidocs", "/static/api/health"])
    def test_a_path_that_only_looks_like_the_api_is_not_guarded(
        self, guarded: TestClient, path: str
    ) -> None:
        assert guarded.get(path).status_code == 404

    def test_the_host_check_still_comes_first(self, guarded: TestClient) -> None:
        foreign = {"host": "evil.example:8787"}
        without = guarded.get("/api/bootstrap", headers=foreign)
        assert without.status_code == 400 and "allowed_hosts" in without.text
        with_cookie = guarded.get(
            "/api/bootstrap", headers={**foreign, **cookie(TOKEN)}
        )
        assert with_cookie.status_code == 400

    def test_the_token_is_checked_before_the_cross_site_rules(
        self, guarded: TestClient
    ) -> None:
        cross_site = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
        body = {"text": "hi"}
        assert (
            guarded.post("/api/chat", json=body, headers=cross_site).status_code == 401
        )
        both = {**cross_site, **cookie(TOKEN)}
        assert guarded.post("/api/chat", json=body, headers=both).status_code == 403
        assert guarded.get("/api/chats", headers=cookie(TOKEN)).json() == {"chats": []}

    def test_a_same_origin_write_with_the_cookie_passes(
        self, guarded: TestClient
    ) -> None:
        headers = {"origin": BASE, "sec-fetch-site": "same-origin", **cookie(TOKEN)}
        response = guarded.post(
            "/api/chat", json={"text": "hi", "model": FREE}, headers=headers
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("token", [None, "", "   "])
    def test_without_a_token_nothing_is_checked(
        self, tmp_path: Path, token: str | None
    ) -> None:
        environ = KEYS if token is None else {**KEYS, "AIHUB_LAUNCH_TOKEN": token}
        app, _ = build(tmp_path, environ=environ)
        with TestClient(app, base_url=BASE) as client:
            assert client.get("/api/health").status_code == 200
            stray = client.get("/api/health", headers=cookie("anything"))
            assert stray.status_code == 200

    @pytest.mark.anyio
    async def test_other_scopes_pass_through_untouched(self) -> None:
        seen = []

        async def inner(scope, receive, send) -> None:
            seen.append(scope["type"])

        guard = SecurityMiddleware(inner, ("127.0.0.1",), token=b"secret")
        for kind in ("websocket", "lifespan"):
            await guard({"type": kind, "path": "/api/x", "headers": []}, None, None)
        assert seen == ["websocket", "lifespan"]


@contextlib.contextmanager
def recording_server(bind: str = "127.0.0.1"):
    """A loopback server that answers the model lists and records what each request asked for.

    Used as an origin it sees paths ("/api/tags"); used as a forward proxy it sees absolute URLs.
    """
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            listing = (
                {"models": []}
                if urlsplit(self.path).path.endswith("/api/tags")
                else {"data": []}
            )
            body = json.dumps(listing).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET

    server = Server((bind, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def set_system_proxy(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, url)
    # Anything else in the developer's shell would change which hosts the proxy is used for.
    for name in (
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


def provider_status(tmp_path: Path, *, ollama: str, openrouter: str) -> dict:
    """What the catalog reports after refreshing from these URLs through the app's real HTTP client."""
    cfg = Config(
        state_dir=tmp_path, ollama={"url": ollama}, openrouter={"url": openrouter}
    )
    with TestClient(create_app(cfg, environ=KEYS), base_url=BASE) as client:
        return client.get("/api/models?refresh=1").json()["providers"]


class TestProviderProxy:
    @pytest.mark.parametrize(
        ("bind", "host"),
        [
            ("127.0.0.1", "127.0.0.1"),
            ("127.0.0.1", "localhost"),
            ("::1", "[::1]"),
        ],
    )
    def test_loopback_calls_ignore_a_system_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind: str, host: str
    ) -> None:
        set_system_proxy(
            monkeypatch, f"http://127.0.0.1:{free_port()}"
        )  # nothing listens
        with recording_server(bind) as (port, seen):
            origin = f"http://{host}:{port}"
            status = provider_status(
                tmp_path, ollama=origin, openrouter=f"{origin}/api/v1"
            )
        assert status["ollama"]["ok"] and status["openrouter"]["ok"], status
        assert "/api/tags" in seen and "/api/v1/models" in seen

    def test_other_hosts_still_go_through_the_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with recording_server() as (proxy_port, via_proxy):
            set_system_proxy(monkeypatch, f"http://127.0.0.1:{proxy_port}")
            with recording_server() as (origin_port, direct):
                status = provider_status(
                    tmp_path,
                    ollama=f"http://127.0.0.1:{origin_port}",
                    openrouter="http://openrouter.example.test/api/v1",
                )
        assert status["ollama"]["ok"] and status["openrouter"]["ok"], status
        assert set(direct) == {"/api/tags"}
        assert set(via_proxy) == {"http://openrouter.example.test/api/v1/models"}


def test_expired_chats_are_deleted_when_the_app_starts(tmp_path: Path) -> None:
    clock = Clock(time.time() - 45 * 86_400)
    seed = Store(tmp_path / "chats.db", clock)
    old = seed.create_chat("ancient", FREE, None)
    seed.add_message(old.id, "user", "from 45 days ago")
    clock.advance(days=44)
    recent = seed.create_chat("recent", FREE, None)
    seed.add_message(recent.id, "user", "from yesterday")
    seed.close()

    app, _ = build(tmp_path)
    with TestClient(app, base_url=BASE) as client:
        ids = [c["id"] for c in client.get("/api/bootstrap").json()["chats"]]
    assert ids == [recent.id]


@pytest.fixture
def live(tmp_path: Path):
    """The real app on a real socket, because a browser dropping the connection can't be simulated in-process."""
    app, fakes = build(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", fakes
    server.should_exit = True
    thread.join(timeout=10)


def wait_for(check, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.02)
    raise AssertionError("condition never became true")


class TestLiveServer:
    def test_dropping_the_connection_keeps_the_partial_reply(self, live) -> None:
        base, fakes = live
        fakes["openrouter"].script = [Text("par"), Text("tial"), HANG]
        with httpx2.Client(base_url=base, timeout=10) as client:
            with client.stream(
                "POST", "/api/chat", json={"text": "q", "model": FREE}
            ) as response:
                lines = response.iter_lines()
                chat_id = json.loads(next(lines))["chat"]["id"]
                seen = ""
                for line in lines:
                    event = json.loads(line)
                    seen += event.get("d", "") if event["t"] == "text" else ""
                    if seen == "partial":
                        break  # leaving the block closes the connection mid-reply

            def stored():
                messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
                return messages if len(messages) == 2 else None

            reply_message = wait_for(stored)[1]
            assert (reply_message["status"], reply_message["content"]) == (
                "stopped",
                "partial",
            )

            fakes["openrouter"].script = [Text("fine")]
            again = client.post("/api/chat", json={"chat_id": chat_id, "text": "next"})
            assert again.status_code == 200 and '"t":"done"' in again.text

    def test_text_arrives_while_the_model_is_still_working(self, live) -> None:
        base, fakes = live
        fakes["openrouter"].script = [Text("first"), HANG]
        with (
            httpx2.Client(base_url=base, timeout=10) as client,
            client.stream(
                "POST", "/api/chat", json={"text": "q", "model": FREE}
            ) as response,
        ):
            lines = response.iter_lines()
            next(lines)
            assert json.loads(next(lines)) == {"t": "text", "d": "first"}


@pytest.mark.anyio
async def test_ending_the_body_closes_the_stream_that_feeds_it(tmp_path: Path) -> None:
    """Without an explicit close, the reply would only be stored whenever the garbage collector got to it."""
    service, store, providers = await make_service(tmp_path)
    providers["openrouter"].script = [Text("par"), HANG]
    turn = service.prepare(ChatRequest(text="q", model=FREE))
    body = ndjson(service.stream(turn))
    await anext(body)  # the chat line
    assert b'"par"' in await anext(body)
    await body.aclose()
    reply_message = store.messages(turn.chat.id)[-1]
    assert (reply_message.status, reply_message.content) == ("stopped", "par")


class TestStaticFiles:
    def test_text_assets_are_compressed_and_the_stream_is_not(
        self, api: TestClient
    ) -> None:
        asset = api.get("/css/app.css", headers={"accept-encoding": "gzip"})
        assert asset.status_code == 200 and asset.headers["content-encoding"] == "gzip"
        assert "text/css" in asset.headers["content-type"]
        with api.stream(
            "POST",
            "/api/chat",
            json={"text": "hi", "model": FREE},
            headers={"accept-encoding": "gzip"},
        ) as response:
            assert "content-encoding" not in response.headers, (
                "gzip would hold back streamed tokens"
            )

    def test_fonts_are_not_compressed_twice(self, api: TestClient) -> None:
        font = api.get(
            "/lib/fonts/instrument-sans-5.3.0/latin-wght-normal.woff2",
            headers={"accept-encoding": "gzip"},
        )
        assert font.status_code == 200 and "content-encoding" not in font.headers
        assert font.headers["content-type"] == "font/woff2"

    def test_app_files_revalidate_and_versioned_libraries_never_do(
        self, api: TestClient
    ) -> None:
        assert api.get("/css/app.css").headers["cache-control"] == "no-cache"
        assert api.get("/js/main.js").headers["cache-control"] == "no-cache"
        lib = api.get("/lib/marked-18.1.0/marked.esm.js")
        assert lib.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_a_conditional_request_gets_a_304_with_no_body(
        self, api: TestClient
    ) -> None:
        first = api.get("/js/main.js")
        again = api.get("/js/main.js", headers={"if-none-match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["cache-control"] == "no-cache"

    def test_javascript_and_manifest_have_the_right_types(
        self, api: TestClient
    ) -> None:
        assert "javascript" in api.get("/js/main.js").headers["content-type"]
        assert (
            api.get("/manifest.webmanifest")
            .headers["content-type"]
            .startswith("application/manifest+json")
        )
        assert api.get("/icon.svg").headers["content-type"].startswith("image/svg+xml")

    def test_every_file_the_page_loads_exists(self, api: TestClient) -> None:
        import re

        html = api.get("/").text
        for path in re.findall(r'(?:href|src)="(/[^"]+)"', html):
            assert api.get(path).status_code == 200, path


IMAGE = "images/zimage"


@pytest.fixture
def image_api(tmp_path: Path, socket_dir: Path):
    """The app with image generation on, talking to a FakeEngine over a real Unix socket."""
    path = str(socket_dir / "engine.sock")
    engine = FakeEngine()
    with serve_engine(engine, path):
        app, fakes = build(tmp_path, images={"socket": path})
        with TestClient(app, base_url=BASE) as client:
            client.fakes, client.engine = fakes, engine
            yield client


def make_image(client: TestClient, text: str = "a red fox", **body) -> tuple[str, dict]:
    """Make one image through the API; returns the chat id and the image's metadata."""
    events = reply(client, text=text, model=IMAGE, **body)
    assert events[-1]["t"] == "done", events[-1]
    return events[0]["chat"]["id"], events[-1]["message"]["images"][0]


class TestImageModelsInTheLists:
    def test_the_lists_are_unchanged_while_no_socket_is_set(
        self, api: TestClient
    ) -> None:
        data = api.get("/api/bootstrap").json()
        assert set(data) == {
            "models",
            "default_model",
            "providers",
            "chats",
            "projects",
            "tasks",
            "settings",
            "retention_days",
        }
        assert set(data["providers"]) == {"ollama", "openrouter", "anthropic"}
        assert not any(m["provider"] == "images" for m in data["models"])
        for model in data["models"]:
            assert not {"output", "edits", "est_s", "note"} & set(model)
        assert data["default_model"] == FREE
        assert (
            api.get("/api/models").json()["providers"].keys()
            == data["providers"].keys()
        )

    def test_a_socket_adds_the_image_models_and_a_status(
        self, image_api: TestClient
    ) -> None:
        data = image_api.get("/api/bootstrap").json()
        images = [m for m in data["models"] if m["provider"] == "images"]
        assert [m["id"] for m in images] == ["images/zimage", "images/klein4b"]
        assert images[0]["est_s"] == {"square": 23, "wide": 12, "tall": 12}
        assert (images[0]["output"], images[0]["edits"], images[1]["edits"]) == (
            "image",
            False,
            True,
        )
        assert all(m["available"] for m in images)
        assert data["providers"]["images"] == "ok"
        assert set(data) == {
            "models",
            "default_model",
            "providers",
            "chats",
            "projects",
            "tasks",
            "settings",
            "retention_days",
        }
        assert data["default_model"] == FREE
        assert image_api.get("/api/models").json()["providers"]["images"] == "ok"

    def test_a_socket_that_is_not_there_lists_them_unavailable(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        app, _ = build(tmp_path, images={"socket": str(socket_dir / "absent.sock")})
        with TestClient(app, base_url=BASE) as client:
            data = client.get("/api/bootstrap").json()
            images = [m for m in data["models"] if m["provider"] == "images"]
            assert len(images) == 2 and not any(m["available"] for m in images)
            assert (
                images[0]["reason"] == "Image engine isn't set up. See README, Images."
            )
            assert data["providers"]["images"] == "down"
            refused = client.post("/api/chat", json={"text": "a fox", "model": IMAGE})
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "model_unavailable"


class TestMakingImagesOverHttp:
    def test_a_prompt_streams_progress_and_then_the_finished_reply(
        self, image_api: TestClient
    ) -> None:
        with image_api.stream(
            "POST",
            "/api/chat",
            json={"text": "a red fox", "model": IMAGE, "shape": "wide"},
        ) as response:
            assert response.headers["content-type"].startswith("application/x-ndjson")
            assert response.headers["cache-control"] == "no-cache, no-transform"
            events = [json.loads(line) for line in response.iter_lines() if line]
        kinds = [e["t"] for e in events]
        assert kinds[0] == "chat" and kinds[-1] == "done"
        progress = [e for e in events if e["t"] == "progress"]
        assert {"t": "progress", "phase": "queued"} in progress
        assert {"t": "progress", "phase": "sampling", "step": 1, "steps": 8} in progress
        assert progress[-1] == {"t": "progress", "phase": "finishing"}
        message = events[-1]["message"]
        assert message["status"] == "done" and message["content"] == ""
        [image] = message["images"]
        assert (
            image["w"],
            image["h"],
            image["steps"],
            image["model"],
            image["ref"],
        ) == (
            1024,
            576,
            8,
            IMAGE,
            None,
        )
        assert set(message["usage"]) == {"total_ms", "steps", "shape"}

    def test_the_chat_lists_the_image_without_its_bytes(
        self, image_api: TestClient
    ) -> None:
        chat_id, image = make_image(image_api)
        detail = image_api.get(f"/api/chats/{chat_id}").json()
        assert [m["images"] for m in detail["messages"]] == [None, [image]]
        assert len(json.dumps(detail)) < 2000
        assert detail["pending_image"] is None

    def test_the_image_comes_back_as_webp_with_the_headers_that_keep_it_private(
        self, image_api: TestClient
    ) -> None:
        chat_id, image = make_image(image_api, shape="wide")
        response = image_api.get(f"/api/chats/{chat_id}/images/{image['id']}")
        assert response.status_code == 200
        assert response.content == webp("wide")
        headers = response.headers
        assert headers["content-type"] == "image/webp"
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cross-origin-resource-policy"] == "same-origin"
        assert headers["content-security-policy"] == CSP
        assert (
            headers["content-disposition"]
            == f'inline; filename="aihub-{image["id"]}.webp"'
        )
        assert "access-control-allow-origin" not in headers

    def test_an_image_is_found_only_under_its_own_chat_and_its_own_id(
        self, image_api: TestClient
    ) -> None:
        chat_id, image = make_image(image_api)
        other_chat, other_image = make_image(image_api, text="another")
        for path in (
            f"/api/chats/{other_chat}/images/{image['id']}",  # another chat's id
            f"/api/chats/{chat_id}/images/{other_image['id']}",
            f"/api/chats/{chat_id}/images/0123456789abcdef",  # unknown image
            "/api/chats/0123456789abcdef/images/" + image["id"],  # unknown chat
            f"/api/chats/{chat_id}/images/{image['id'].upper()}",  # not the canonical form
            f"/api/chats/{chat_id}/images/{image['id'][:15]}",
            f"/api/chats/{chat_id}/images/{image['id']}0",
            f"/api/chats/{chat_id}/images/..%2f..%2fetc%2fpasswd",
            f"/api/chats/{chat_id}/images/%2e%2e",
            f"/api/chats/{chat_id.upper()}/images/{image['id']}",
        ):
            response = image_api.get(path)
            assert response.status_code == 404, path
            if response.headers["content-type"].startswith("application/json"):
                assert response.json()["error"]["code"] in ("no_image", "not_found")
        missing = image_api.get(f"/api/chats/{chat_id}/images/0123456789abcdef")
        assert missing.json()["error"] == {
            "code": "no_image",
            "message": "That image no longer exists.",
        }

    def test_only_ids_in_the_canonical_form_are_looked_up(
        self, image_api: TestClient, tmp_path: Path
    ) -> None:
        """Ids the app makes are 16 lowercase hex digits. A row that somehow has another kind is not served."""
        raw = sqlite3.connect(tmp_path / "chats.db")
        raw.execute("PRAGMA foreign_keys = ON")
        # (chat id, image id): one pair with a wrong image id, one with a wrong chat id
        rows = [
            ("abcdef0123456789", "FEDCBA9876543210"),
            ("ABCDEF0123456790", "feedface00112233"),
        ]
        for n, (chat_id, image_id) in enumerate(rows):
            raw.execute(
                "INSERT INTO chats (id, title, model, effort, created_at, updated_at)"
                " VALUES (?, 't', 'm', NULL, 1, 1)",
                (chat_id,),
            )
            raw.execute(
                "INSERT INTO messages VALUES (?, ?, 1, 'assistant', '', NULL, NULL, NULL, NULL, 'done', NULL, 1)",
                (f"message{n:09d}", chat_id),
            )
            raw.execute(
                "INSERT INTO images VALUES (?, ?, ?, 'image/webp', 1, 1, 1, 1, 'm', NULL, 4, X'52494646', 1)",
                (image_id, f"message{n:09d}", chat_id),
            )
        raw.commit()
        raw.close()
        for chat_id, image_id in rows:
            response = image_api.get(f"/api/chats/{chat_id}/images/{image_id}")
            assert response.status_code == 404, (chat_id, image_id)

    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    def test_a_fetch_from_another_site_is_refused(
        self, image_api: TestClient, site: str
    ) -> None:
        chat_id, image = make_image(image_api)
        path = f"/api/chats/{chat_id}/images/{image['id']}"
        refused = image_api.get(path, headers={"sec-fetch-site": site})
        assert refused.status_code == 403
        assert refused.content != webp("square")
        assert refused.headers["cache-control"] == "no-store"
        for allowed in ("same-origin", "none"):
            assert (
                image_api.get(path, headers={"sec-fetch-site": allowed}).status_code
                == 200
            )
        assert (
            image_api.get(path).status_code == 200
        )  # no header: not a browser's embedded fetch

    def test_the_launch_token_covers_the_image_route(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        path = str(socket_dir / "engine.sock")
        with serve_engine(FakeEngine(), path):
            app, _ = build(
                tmp_path,
                environ={**KEYS, "AIHUB_LAUNCH_TOKEN": TOKEN},
                images={"socket": path},
            )
            with TestClient(app, base_url=BASE) as client:
                with client.stream(
                    "POST",
                    "/api/chat",
                    json={"text": "a fox", "model": IMAGE},
                    headers=cookie(TOKEN),
                ) as response:
                    events = [
                        json.loads(line) for line in response.iter_lines() if line
                    ]
                chat_id = events[0]["chat"]["id"]
                image_id = events[-1]["message"]["images"][0]["id"]
                url = f"/api/chats/{chat_id}/images/{image_id}"
                assert client.get(url).status_code == 401
                assert client.get(url, headers=cookie("wrong")).status_code == 401
                assert client.get(url, headers=cookie(TOKEN)).status_code == 200
                assert (
                    client.post(f"/api/chats/{chat_id}/image/stop").status_code == 401
                )

    def test_deleting_the_chat_deletes_its_images(self, image_api: TestClient) -> None:
        chat_id, image = make_image(image_api)
        url = f"/api/chats/{chat_id}/images/{image['id']}"
        assert image_api.get(url).status_code == 200
        assert image_api.delete(f"/api/chats/{chat_id}").status_code == 204
        assert image_api.get(url).status_code == 404

    def test_deleting_everything_deletes_every_image(
        self, image_api: TestClient
    ) -> None:
        make_image(image_api, text="one")
        chat_id, second = make_image(image_api, text="two")
        image_api.request("DELETE", "/api/chats", json={"confirm": "delete-all"})
        assert (
            image_api.get(f"/api/chats/{chat_id}/images/{second['id']}").status_code
            == 404
        )

    def test_retrying_replaces_the_image(self, image_api: TestClient) -> None:
        events = reply(image_api, text="a red fox", model=IMAGE)
        chat_id, user_id = events[0]["chat"]["id"], events[0]["user"]["id"]
        old = events[-1]["message"]["images"][0]["id"]
        again = reply(image_api, chat_id=chat_id, regenerate_for=user_id)
        new = again[-1]["message"]["images"][0]["id"]
        assert new != old
        assert image_api.get(f"/api/chats/{chat_id}/images/{old}").status_code == 404
        assert image_api.get(f"/api/chats/{chat_id}/images/{new}").status_code == 200

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"text": "x" * 2001, "model": IMAGE}, 413, "prompt_too_long"),
            ({"text": "a fox", "model": IMAGE, "shape": "huge"}, 422, "invalid"),
            ({"text": "hi", "model": FREE, "shape": "wide"}, 422, "invalid"),
            (
                {"text": "a fox", "model": IMAGE, "reference": "0123456789abcdef"},
                422,
                "invalid",
            ),
        ],
    )
    def test_refusals_come_as_json_before_anything_streams(
        self, image_api: TestClient, body: dict, status: int, code: str
    ) -> None:
        response = image_api.post("/api/chat", json=body)
        assert (response.status_code, response.json()["error"]["code"]) == (
            status,
            code,
        )
        assert image_api.get("/api/chats").json()["chats"] == []


class TestStoppingAnImage:
    def test_there_is_nothing_to_stop_when_nothing_is_being_made(
        self, image_api: TestClient
    ) -> None:
        chat_id, _ = make_image(image_api)
        response = image_api.post(f"/api/chats/{chat_id}/image/stop")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "no_job"
        unknown = image_api.post("/api/chats/0123456789abcdef/image/stop")
        assert (unknown.status_code, unknown.json()["error"]["code"]) == (
            404,
            "no_chat",
        )

    def test_the_body_is_empty_or_an_empty_object_and_nothing_else(
        self, image_api: TestClient
    ) -> None:
        chat_id, _ = make_image(image_api)
        url = f"/api/chats/{chat_id}/image/stop"
        assert (
            image_api.post(url, json={}).status_code == 404
        )  # no job, but the body was fine
        assert image_api.post(url, json={"force": True}).status_code == 422
        assert (
            image_api.post(
                url, content="[]", headers={"content-type": "application/json"}
            ).status_code
            == 422
        )
        assert (
            image_api.post(
                url, content="{nope", headers={"content-type": "application/json"}
            ).status_code
            == 400
        )
        assert (
            image_api.post(
                url, content="x", headers={"content-type": "text/plain"}
            ).status_code
            == 415
        )

    def test_without_image_generation_the_route_has_nothing_to_stop(
        self, api: TestClient
    ) -> None:
        chat_id = reply(api, text="hi", model=FREE)[0]["chat"]["id"]
        response = api.post(f"/api/chats/{chat_id}/image/stop")
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "no_job",
        )
        assert "pending_image" not in api.get(f"/api/chats/{chat_id}").json()

    def test_a_cross_site_post_is_refused_like_any_write(
        self, image_api: TestClient
    ) -> None:
        chat_id, _ = make_image(image_api)
        response = image_api.post(
            f"/api/chats/{chat_id}/image/stop", headers={"sec-fetch-site": "cross-site"}
        )
        assert response.status_code == 403


@contextlib.contextmanager
def running(app):
    """The app on a real socket, because a browser dropping the connection can't be simulated in-process."""
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def live_images(tmp_path: Path, socket_dir: Path):
    path = str(socket_dir / "engine.sock")
    engine = FakeEngine()
    with serve_engine(engine, path):
        app, _ = build(tmp_path, images={"socket": path})
        with running(app) as base:
            yield base, engine


class TestImagesOnARealServer:
    def test_dropping_the_connection_still_saves_the_image(self, live_images) -> None:
        """Starlette cancels the response task when the client goes away. The job is not part of it."""
        base, engine = live_images
        engine.scripts.append([generating(1, 8), HOLD, finished])
        with httpx2.Client(base_url=base, timeout=10) as client:
            with client.stream(
                "POST", "/api/chat", json={"text": "a red fox", "model": IMAGE}
            ) as response:
                lines = response.iter_lines()
                chat_id = json.loads(next(lines))["chat"]["id"]
                assert json.loads(next(lines))["phase"] == "sampling"
            # leaving the block closed the connection in the middle of the job
            pending = client.get(f"/api/chats/{chat_id}").json()["pending_image"]
            assert pending["phase"] == "sampling" and pending["model"] == IMAGE
            engine.release()

            def saved():
                messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
                return messages if len(messages) == 2 else None

            reply_message = wait_for(saved)[1]
            assert reply_message["status"] == "done"
            image_id = reply_message["images"][0]["id"]
            assert client.get(
                f"/api/chats/{chat_id}/images/{image_id}"
            ).content == webp("square")
            assert client.get(f"/api/chats/{chat_id}").json()["pending_image"] is None
        assert (engine.kills, engine.cancels) == (0, [])

    def test_stop_ends_the_open_stream_with_a_stopped_reply(self, live_images) -> None:
        base, engine = live_images
        engine.scripts.append([generating(2, 8)])
        with httpx2.Client(base_url=base, timeout=10) as client:
            with client.stream(
                "POST", "/api/chat", json={"text": "a fox", "model": IMAGE}
            ) as response:
                lines = response.iter_lines()
                chat_id = json.loads(next(lines))["chat"]["id"]
                assert json.loads(next(lines)) == {
                    "t": "progress",
                    "phase": "sampling",
                    "step": 2,
                    "steps": 8,
                }
                pending = client.get(f"/api/chats/{chat_id}").json()["pending_image"]
                assert pending == {
                    "model": IMAGE,
                    "shape": "square",
                    "phase": "sampling",
                    "step": 2,
                    "steps": 8,
                }
                stop = client.post(f"/api/chats/{chat_id}/image/stop")
                assert (stop.status_code, stop.json()) == (200, {"stopping": True})
                rest = [json.loads(line) for line in lines if line]
            assert (
                rest[-1]["t"] == "done" and rest[-1]["message"]["status"] == "stopped"
            )
            assert rest[-1]["message"]["images"] is None
            assert client.get(f"/api/chats/{chat_id}").json()["pending_image"] is None
            assert client.post(f"/api/chats/{chat_id}/image/stop").status_code == 404
        assert (engine.kills, engine.cancels) == (1, [])

    def test_a_second_browser_window_sees_the_job_and_can_stop_it(
        self, live_images
    ) -> None:
        base, engine = live_images
        engine.scripts.append([generating(1, 8)])
        with (
            httpx2.Client(base_url=base, timeout=10) as first,
            httpx2.Client(base_url=base, timeout=10) as second,
            first.stream(
                "POST", "/api/chat", json={"text": "a fox", "model": IMAGE}
            ) as response,
        ):
            lines = response.iter_lines()
            chat_id = json.loads(next(lines))["chat"]["id"]
            next(lines)
            assert (
                second.get(f"/api/chats/{chat_id}").json()["pending_image"]["phase"]
                == "sampling"
            )
            assert second.post(f"/api/chats/{chat_id}/image/stop").json() == {
                "stopping": True
            }
            assert json.loads(list(lines)[-1])["message"]["status"] == "stopped"


def new_project(client: TestClient, name: str = "Trips", **fields) -> dict:
    response = client.post("/api/projects", json={"name": name, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


class TestProjectsApi:
    def test_there_are_none_to_begin_with(self, api: TestClient) -> None:
        assert api.get("/api/projects").json() == {"projects": []}
        assert api.get("/api/bootstrap").json()["projects"] == []

    def test_a_new_project_is_listed_and_in_the_bootstrap(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "  My   trips ", instructions="  Answer in French.\n")
        assert (
            made["name"] == "My trips" and made["instructions"] == "Answer in French."
        )
        assert made["chat_count"] == 0 and re.fullmatch(r"[0-9a-f]{16}", made["id"])
        assert api.get("/api/projects").json() == {"projects": [made]}
        assert api.get("/api/bootstrap").json()["projects"] == [made]

    def test_the_list_is_by_name(self, api: TestClient) -> None:
        for name in ("beta", "Alpha", "Zed"):
            new_project(api, name)
        names = [p["name"] for p in api.get("/api/projects").json()["projects"]]
        assert names == ["Alpha", "beta", "Zed"]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"name": ""},
            {"name": "   \n\t "},
            {"name": "x" * 81},
            {"name": 7},
            {"name": "ok", "instructions": "x" * 4001},
            {"name": "ok", "colour": "red"},
        ],
    )
    def test_a_project_that_does_not_fit_is_refused(
        self, api: TestClient, body: dict
    ) -> None:
        response = api.post("/api/projects", json=body)
        assert error_of(response) == (422, "invalid")
        assert api.get("/api/projects").json() == {"projects": []}

    def test_a_name_may_be_eighty_characters_and_instructions_four_thousand(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "n" * 80, instructions="i" * 4000)
        assert len(made["name"]) == 80 and len(made["instructions"]) == 4000

    def test_a_body_that_is_not_json_is_refused(self, api: TestClient) -> None:
        response = api.post(
            "/api/projects",
            content=b"{nope",
            headers={"content-type": "application/json"},
        )
        assert error_of(response) == (400, "bad_json")

    def test_there_is_a_limit_of_a_hundred(self, api: TestClient) -> None:
        for n in range(100):
            new_project(api, f"p{n:03d}")
        refused = api.post("/api/projects", json={"name": "one too many"})
        assert error_of(refused) == (409, "too_many_projects")
        assert len(api.get("/api/projects").json()["projects"]) == 100

    def test_editing_changes_what_is_sent(self, api: TestClient) -> None:
        made = new_project(api, "Trips", instructions="Be brief.")
        url = f"/api/projects/{made['id']}"
        renamed = api.patch(url, json={"name": " Travel  plans "}).json()
        assert (renamed["name"], renamed["instructions"]) == (
            "Travel plans",
            "Be brief.",
        )
        rewritten = api.patch(url, json={"instructions": ""}).json()
        assert (rewritten["name"], rewritten["instructions"]) == ("Travel plans", "")
        both = api.patch(url, json={"name": "Trips", "instructions": " x "}).json()
        assert (both["name"], both["instructions"]) == ("Trips", "x")
        assert api.get("/api/projects").json()["projects"] == [both]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"name": ""},
            {"name": "  "},
            {"name": None},
            {"instructions": "x" * 4001},
            {"id": "x"},
        ],
    )
    def test_an_edit_that_does_not_fit_is_refused(
        self, api: TestClient, body: dict
    ) -> None:
        made = new_project(api, "Trips", instructions="Be brief.")
        response = api.patch(f"/api/projects/{made['id']}", json=body)
        assert error_of(response) == (422, "invalid")
        assert api.get("/api/projects").json()["projects"] == [made]

    @pytest.mark.parametrize(
        "project_id", ["0123456789abcdef", "nope", "0123456789ABCDEF"]
    )
    def test_an_unknown_project_is_a_404_for_every_verb(
        self, api: TestClient, project_id: str
    ) -> None:
        url = f"/api/projects/{project_id}"
        assert error_of(api.patch(url, json={"name": "x"})) == (404, "no_project")
        assert error_of(api.delete(url)) == (404, "no_project")

    def test_deleting_keeps_the_chats_unless_told_otherwise(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "Trips")
        chat_id = reply(api, text="hello", model=FREE, project_id=made["id"])[0][
            "chat"
        ]["id"]
        assert api.delete(f"/api/projects/{made['id']}").status_code == 204
        kept = api.get(f"/api/chats/{chat_id}").json()["chat"]
        assert kept["project_id"] is None
        assert api.get("/api/projects").json() == {"projects": []}
        assert error_of(api.delete(f"/api/projects/{made['id']}")) == (
            404,
            "no_project",
        )

    def test_deleting_can_take_the_projects_chats_and_only_those(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "Trips")
        other = new_project(api, "Work")
        mine = reply(api, text="in trips", model=FREE, project_id=made["id"])[0][
            "chat"
        ]["id"]
        theirs = reply(api, text="in work", model=FREE, project_id=other["id"])[0][
            "chat"
        ]["id"]
        free = reply(api, text="alone", model=FREE)[0]["chat"]["id"]
        response = api.request(
            "DELETE", f"/api/projects/{made['id']}", json={"chats": "delete"}
        )
        assert response.status_code == 204
        assert error_of(api.get(f"/api/chats/{mine}")) == (404, "no_chat")
        assert (
            api.get(f"/api/chats/{theirs}").json()["chat"]["project_id"] == other["id"]
        )
        assert api.get(f"/api/chats/{free}").status_code == 200

    @pytest.mark.parametrize("body", [{"chats": "all"}, {"chats": True}, {"keep": 1}])
    def test_a_delete_that_does_not_make_sense_is_refused(
        self, api: TestClient, body: dict
    ) -> None:
        made = new_project(api, "Trips")
        response = api.request("DELETE", f"/api/projects/{made['id']}", json=body)
        assert error_of(response) == (422, "invalid")
        assert len(api.get("/api/projects").json()["projects"]) == 1

    def test_delete_all_chats_leaves_the_projects(self, api: TestClient) -> None:
        made = new_project(api, "Trips", instructions="Be brief.")
        reply(api, text="hello", model=FREE, project_id=made["id"])
        api.request("DELETE", "/api/chats", json={"confirm": "delete-all"})
        assert api.get("/api/projects").json()["projects"][0]["chat_count"] == 0
        assert api.get("/api/projects").json()["projects"][0]["name"] == "Trips"


class TestChatsInProjects:
    def test_a_new_chat_goes_in_the_project_and_the_model_hears_its_instructions(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "Trips", instructions="Answer in French.")
        events = reply(api, text="hello", model=FREE, project_id=made["id"])
        assert events[0]["chat"]["project_id"] == made["id"]
        assert 'Instructions for the project "Trips":\nAnswer in French.' in (
            api.fakes["openrouter"].requests[-1].system
        )
        assert api.get("/api/projects").json()["projects"][0]["chat_count"] == 1
        assert [c["project_id"] for c in api.get("/api/bootstrap").json()["chats"]] == [
            made["id"]
        ]

    def test_a_chat_with_no_project_has_a_null_one(self, api: TestClient) -> None:
        events = reply(api, text="hello", model=FREE)
        assert events[0]["chat"]["project_id"] is None

    def test_only_a_new_chat_takes_a_project(self, api: TestClient) -> None:
        made = new_project(api, "Trips")
        chat_id = reply(api, text="first", model=FREE)[0]["chat"]["id"]
        response = api.post(
            "/api/chat",
            json={"text": "again", "chat_id": chat_id, "project_id": made["id"]},
        )
        assert error_of(response) == (422, "invalid")
        assert api.get(f"/api/chats/{chat_id}").json()["chat"]["project_id"] is None

    def test_a_project_that_does_not_exist_makes_no_chat(self, api: TestClient) -> None:
        response = api.post(
            "/api/chat",
            json={"text": "hello", "model": FREE, "project_id": "0123456789abcdef"},
        )
        assert error_of(response) == (404, "no_project")
        assert api.get("/api/bootstrap").json()["chats"] == []
        assert not api.fakes["openrouter"].requests

    @pytest.mark.parametrize("bad", ["", "nope", "0123456789ABCDEF", "../../etc"])
    def test_a_project_id_has_one_form(self, api: TestClient, bad: str) -> None:
        response = api.post(
            "/api/chat", json={"text": "hello", "model": FREE, "project_id": bad}
        )
        assert error_of(response) == (422, "invalid")

    def test_a_chat_can_be_moved_into_a_project_and_out_without_counting_as_activity(
        self, api: TestClient
    ) -> None:
        made = new_project(api, "Trips")
        chat = reply(api, text="hello", model=FREE)[0]["chat"]
        url = f"/api/chats/{chat['id']}"
        moved = api.patch(url, json={"project_id": made["id"]}).json()
        assert moved["project_id"] == made["id"]
        assert moved["updated_at"] == api.get(url).json()["chat"]["updated_at"]
        assert api.get("/api/projects").json()["projects"][0]["chat_count"] == 1
        free = api.patch(url, json={"project_id": None}).json()
        assert free["project_id"] is None
        assert api.get("/api/projects").json()["projects"][0]["chat_count"] == 0

    def test_renaming_still_works_alone_and_with_a_move(self, api: TestClient) -> None:
        made = new_project(api, "Trips")
        chat_id = reply(api, text="hello", model=FREE)[0]["chat"]["id"]
        url = f"/api/chats/{chat_id}"
        assert (
            api.patch(url, json={"title": "  A  new   title "}).json()["title"]
            == "A new title"
        )
        both = api.patch(url, json={"title": "Both", "project_id": made["id"]}).json()
        assert (both["title"], both["project_id"]) == ("Both", made["id"])

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"title": None},
            {"title": ""},
            {"title": "  \n "},
            {"title": "t" * 201},
            {"project_id": "nope"},
            {"model": "x"},
        ],
    )
    def test_a_change_that_does_not_make_sense_is_refused(
        self, api: TestClient, body: dict
    ) -> None:
        chat_id = reply(api, text="hello", model=FREE)[0]["chat"]["id"]
        before = api.get(f"/api/chats/{chat_id}").json()["chat"]
        assert error_of(api.patch(f"/api/chats/{chat_id}", json=body)) == (
            422,
            "invalid",
        )
        assert api.get(f"/api/chats/{chat_id}").json()["chat"] == before

    def test_moving_a_chat_to_a_project_that_does_not_exist_is_a_404(
        self, api: TestClient
    ) -> None:
        chat_id = reply(api, text="hello", model=FREE)[0]["chat"]["id"]
        response = api.patch(
            f"/api/chats/{chat_id}", json={"project_id": "0123456789abcdef"}
        )
        assert error_of(response) == (404, "no_project")
        assert api.get(f"/api/chats/{chat_id}").json()["chat"]["project_id"] is None


DAILY = {"kind": "daily", "time": "08:00"}
NOW = utc_ms("2027-01-15 08:00")  # where the Clock starts
TOMORROW = utc_ms("2027-01-16 08:00")


BASE_TASK = {"name": "x", "prompt": "p", "model": LOCAL, "schedule": DAILY}


def new_task(client: TestClient, name: str = "Morning digest", **fields) -> dict:
    body = {
        "name": name,
        "prompt": "Summarise my day.",
        "model": LOCAL,
        "schedule": DAILY,
        **fields,
    }
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def tasks_of(client: TestClient) -> list[dict]:
    return client.get("/api/tasks").json()["tasks"]


def seeded_task(store: Store, **fields):
    fields.setdefault("model", LOCAL)
    return store.create_task(
        "Morning digest",
        prompt="Summarise my day.",
        effort=None,
        schedule=DAILY,
        **fields,
    )


@contextlib.contextmanager
def tasks_app(tmp_path: Path, tz, *, seed=None, tick: float = 3600.0, **config):
    """The app on a clock that starts at 2027-01-15 08:00 UTC, with a tick so long that the scheduler leaves
    tasks alone unless a test asks for it. `seed(store)` fills the database before the app starts."""
    tz("UTC")
    if seed:
        store = Store(tmp_path / "chats.db")
        seed(store)
        store.close()
    clock = Clock()
    app, fakes = build(tmp_path, options={"clock": clock, "task_tick": tick}, **config)
    with TestClient(app, base_url=BASE) as client:
        client.fakes, client.clock = fakes, clock
        yield client


@pytest.fixture
def tasks_api(tmp_path: Path, tz):
    with tasks_app(tmp_path, tz) as client:
        yield client


def finished_task(client: TestClient, index: int = 0) -> dict | None:
    task = tasks_of(client)[index]
    return task if task["last_status"] and not task["running"] else None


class TestTasksApi:
    def test_there_are_none_to_begin_with(self, tasks_api: TestClient) -> None:
        assert tasks_api.get("/api/tasks").json() == {"tasks": []}
        assert tasks_api.get("/api/bootstrap").json()["tasks"] == []

    def test_a_new_task_is_listed_and_in_the_bootstrap(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(
            tasks_api,
            "  Morning   digest ",
            prompt="  Summarise my day.\n",
            effort="on",
        )
        assert (made["name"], made["prompt"]) == ("Morning digest", "Summarise my day.")
        assert (made["model"], made["effort"], made["schedule"]) == (LOCAL, "on", DAILY)
        assert made["schedule_text"] == "Every day at 08:00"
        assert (made["enabled"], made["next_run_at"]) == (True, TOMORROW)
        assert (made["last_status"], made["last_chat_id"]) == (None, None)
        assert (made["failures"], made["running"], made["project_id"]) == (
            0,
            False,
            None,
        )
        assert made["switched_off"] is False
        assert re.fullmatch(r"[0-9a-f]{16}", made["id"])
        assert tasks_of(tasks_api) == [made]
        assert tasks_api.get("/api/bootstrap").json()["tasks"] == [made]

    def test_the_list_is_by_name(self, tasks_api: TestClient) -> None:
        for name in ("beta", "Alpha", "Zed"):
            new_task(tasks_api, name)
        assert [t["name"] for t in tasks_of(tasks_api)] == ["Alpha", "beta", "Zed"]

    def test_a_task_made_while_off_has_no_next_run(self, tasks_api: TestClient) -> None:
        made = new_task(tasks_api, enabled=False)
        assert (made["enabled"], made["next_run_at"]) == (False, None)

    @pytest.mark.parametrize(
        "change",
        [
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 81},
            {"name": 7},
            {"prompt": ""},
            {"prompt": " \n "},
            {"model": ""},
            {"model": "m" * 201},
            {"effort": "x" * 17},
            {"schedule": {"kind": "hourly", "every": 5}},
            {"schedule": {"kind": "daily", "time": "8:00"}},
            {"schedule": {"kind": "weekly", "day": 7, "time": "08:00"}},
            {"schedule": {"kind": "daily", "time": "08:00", "zone": "UTC"}},
            {"schedule": "daily"},
            {"project_id": "nope"},
            {"enabled": "yes"},
            {"colour": "red"},
            {"name": None},
            {"prompt": None},
            {"model": None},
            {"schedule": None},
        ],
    )
    def test_a_task_that_does_not_fit_is_refused(
        self, tasks_api: TestClient, change: dict
    ) -> None:
        response = tasks_api.post("/api/tasks", json={**BASE_TASK, **change})
        assert error_of(response) == (422, "invalid")
        assert tasks_of(tasks_api) == []

    @pytest.mark.parametrize("missing", ["name", "prompt", "model", "schedule"])
    def test_every_part_is_needed(self, tasks_api: TestClient, missing: str) -> None:
        body = {k: v for k, v in BASE_TASK.items() if k != missing}
        assert error_of(tasks_api.post("/api/tasks", json=body)) == (422, "invalid")

    def test_a_prompt_over_the_limit_is_refused(self, tmp_path: Path, tz) -> None:
        with tasks_app(tmp_path, tz, limits=Limits(max_message_chars=20)) as client:
            response = client.post("/api/tasks", json={**BASE_TASK, "prompt": "x" * 21})
            assert error_of(response) == (413, "too_long")
            assert response.json()["error"]["message"] == (
                "A prompt is limited to 20 characters."
            )
            new_task(client, prompt="x" * 20)  # exactly the limit is fine

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (FREE, (422, "task_model")),
            (HAIKU, (422, "task_model")),
            ("ollama/not-installed:1b", (400, "unknown_model")),
        ],
    )
    def test_only_a_model_on_this_pc_is_accepted(
        self, tasks_api: TestClient, model: str, expected: tuple[int, str]
    ) -> None:
        response = tasks_api.post("/api/tasks", json={**BASE_TASK, "model": model})
        assert error_of(response) == expected
        assert tasks_of(tasks_api) == []
        if expected[1] == "task_model":
            assert response.json()["error"]["message"] == (
                "A scheduled task can only use a text model that runs on this PC."
            )

    def test_a_project_must_exist(self, tasks_api: TestClient) -> None:
        missing = tasks_api.post(
            "/api/tasks", json={**BASE_TASK, "project_id": "0123456789abcdef"}
        )
        assert error_of(missing) == (404, "no_project")
        project = new_project(tasks_api, "Home")
        made = new_task(tasks_api, project_id=project["id"])
        assert made["project_id"] == project["id"]

    def test_there_is_room_for_fifty(self, tasks_api: TestClient) -> None:
        for n in range(50):
            new_task(tasks_api, f"Task {n:02}")
        response = tasks_api.post("/api/tasks", json=BASE_TASK)
        assert error_of(response) == (409, "too_many_tasks")
        assert len(tasks_of(tasks_api)) == 50


class TestEditingTasks:
    def test_fields_can_be_changed_one_at_a_time(self, tasks_api: TestClient) -> None:
        made = new_task(tasks_api)
        project = new_project(tasks_api, "Home")
        url = f"/api/tasks/{made['id']}"

        changed = tasks_api.patch(
            url,
            json={
                "name": " New  name ",
                "prompt": " Other. ",
                "effort": "off",
                "project_id": project["id"],
            },
        ).json()

        assert (changed["name"], changed["prompt"], changed["effort"]) == (
            "New name",
            "Other.",
            "off",
        )
        assert changed["project_id"] == project["id"]
        assert (changed["schedule"], changed["next_run_at"]) == (DAILY, TOMORROW)
        cleared = tasks_api.patch(url, json={"effort": None, "project_id": None}).json()
        assert (cleared["effort"], cleared["project_id"]) == (None, None)
        assert tasks_of(tasks_api) == [cleared]

    def test_a_new_schedule_moves_the_next_run_from_now(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(tasks_api)
        changed = tasks_api.patch(
            f"/api/tasks/{made['id']}",
            json={"schedule": {"kind": "hourly", "every": 2}},
        ).json()
        assert changed["schedule_text"] == "Every 2 hours"
        assert changed["next_run_at"] == utc_ms("2027-01-15 10:00")

    def test_a_new_schedule_for_a_task_that_is_off_leaves_it_off(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(tasks_api, enabled=False)
        changed = tasks_api.patch(
            f"/api/tasks/{made['id']}",
            json={"schedule": {"kind": "hourly", "every": 2}},
        ).json()
        assert (changed["enabled"], changed["next_run_at"]) == (False, None)

    def test_switching_off_clears_the_next_run_and_on_sets_it_from_now(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(tasks_api)
        url = f"/api/tasks/{made['id']}"
        off = tasks_api.patch(url, json={"enabled": False}).json()
        assert (off["enabled"], off["next_run_at"]) == (False, None)
        tasks_api.clock.advance(days=2, seconds=3600)  # 2027-01-17 09:00
        on = tasks_api.patch(url, json={"enabled": True}).json()
        assert (on["enabled"], on["next_run_at"]) == (True, utc_ms("2027-01-18 08:00"))

    def test_saying_on_again_leaves_the_next_run_alone(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(tasks_api)
        tasks_api.clock.advance(3600)
        again = tasks_api.patch(
            f"/api/tasks/{made['id']}", json={"enabled": True}
        ).json()
        assert again["next_run_at"] == TOMORROW

    def test_switching_on_after_three_failures_starts_afresh(
        self, tmp_path: Path, tz
    ) -> None:
        def seed(store: Store) -> None:
            task = seeded_task(store, enabled=False)
            store.update_task(
                task.id,
                {
                    "failures": 3,
                    "last_status": "error",
                    "last_error": "Switched off after 3 failed runs in a row. The last one: Gone.",
                    "last_run_at": NOW,
                },
            )

        with tasks_app(tmp_path, tz, seed=seed) as client:
            (task,) = tasks_of(client)
            on = client.patch(f"/api/tasks/{task['id']}", json={"enabled": True}).json()
            assert (on["failures"], on["last_status"], on["last_error"]) == (
                0,
                None,
                None,
            )
            assert (on["last_run_at"], on["next_run_at"]) == (None, TOMORROW)

    def test_a_task_says_whether_the_scheduler_switched_it_off(
        self, tmp_path: Path, tz
    ) -> None:
        """So that the page can tell it from a task that was paused by hand, without knowing the words."""
        gone = "The last one: Gone."

        def seed(store: Store) -> None:
            for name, enabled, error in (
                (
                    "Switched off",
                    False,
                    f"Switched off after 3 failed runs in a row. {gone}",
                ),
                ("Paused", False, "Gone."),
                (
                    "Running again",
                    True,
                    f"Switched off after 3 failed runs in a row. {gone}",
                ),
            ):
                task = seeded_task(store, enabled=enabled)
                store.update_task(task.id, {"name": name, "last_error": error})

        with tasks_app(tmp_path, tz, seed=seed) as client:
            flags = {t["name"]: t["switched_off"] for t in tasks_of(client)}
        assert flags == {"Switched off": True, "Paused": False, "Running again": False}

    def test_pausing_by_hand_keeps_the_last_result(self, tmp_path: Path, tz) -> None:
        def seed(store: Store) -> None:
            task = seeded_task(store, next_run_at=TOMORROW)
            store.update_task(task.id, {"last_status": "done", "last_run_at": NOW})

        with tasks_app(tmp_path, tz, seed=seed) as client:
            (task,) = tasks_of(client)
            url = f"/api/tasks/{task['id']}"
            client.patch(url, json={"enabled": False})
            on = client.patch(url, json={"enabled": True}).json()
            assert (on["last_status"], on["last_run_at"], on["failures"]) == (
                "done",
                NOW,
                0,
            )

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"name": None},
            {"prompt": None},
            {"model": None},
            {"schedule": None},
            {"enabled": None},
            {"name": ""},
            {"prompt": "   "},
            {"schedule": {"kind": "daily"}},
            {"project_id": "nope"},
            {"colour": "red"},
        ],
    )
    def test_an_edit_that_does_not_fit_is_refused(
        self, tasks_api: TestClient, body: dict
    ) -> None:
        made = new_task(tasks_api)
        response = tasks_api.patch(f"/api/tasks/{made['id']}", json=body)
        assert error_of(response) == (422, "invalid")
        assert tasks_of(tasks_api) == [made]

    def test_an_edit_gets_the_checks_of_creation(self, tmp_path: Path, tz) -> None:
        with tasks_app(tmp_path, tz, limits=Limits(max_message_chars=20)) as client:
            made = new_task(client)
            url = f"/api/tasks/{made['id']}"
            assert error_of(client.patch(url, json={"model": FREE})) == (
                422,
                "task_model",
            )
            assert error_of(client.patch(url, json={"model": "ollama/nope:1b"})) == (
                400,
                "unknown_model",
            )
            assert error_of(
                client.patch(url, json={"project_id": "0123456789abcdef"})
            ) == (404, "no_project")
            assert error_of(client.patch(url, json={"prompt": "x" * 21})) == (
                413,
                "too_long",
            )
            assert tasks_of(client) == [made]

    @pytest.mark.parametrize("task_id", ["0123456789abcdef", "x"])
    def test_a_task_that_does_not_exist_is_not_found(
        self, tasks_api: TestClient, task_id: str
    ) -> None:
        for response in (
            tasks_api.patch(f"/api/tasks/{task_id}", json={"enabled": False}),
            tasks_api.delete(f"/api/tasks/{task_id}"),
            tasks_api.post(f"/api/tasks/{task_id}/run"),
        ):
            assert error_of(response) in {(404, "no_task"), (404, "not_found")}


class TestRunningTasks:
    def test_run_now_answers_at_once_with_the_chat(self, tasks_api: TestClient) -> None:
        tasks_api.fakes["ollama"].script = [Text("Do the laundry."), Finish("stop")]
        made = new_task(tasks_api)

        response = tasks_api.post(f"/api/tasks/{made['id']}/run")

        assert response.status_code == 202
        chat_id = response.json()["chat_id"]
        done = wait_for(lambda: finished_task(tasks_api))
        assert (done["last_status"], done["last_chat_id"]) == ("done", chat_id)
        assert done["next_run_at"] == made["next_run_at"]  # the schedule is left alone
        body = tasks_api.get(f"/api/chats/{chat_id}").json()
        assert body["chat"]["title"] == "Morning digest, 15 Jan 2027"
        assert body["chat"]["task_id"] == made["id"]
        assert body["messages"][-1]["content"] == "Do the laundry."
        assert any(
            c["task_id"] == made["id"]
            for c in tasks_api.get("/api/chats").json()["chats"]
        )

    def test_the_list_says_which_task_is_running(self, tasks_api: TestClient) -> None:
        started = threading.Event()
        tasks_api.fakes["ollama"].script = [Text("Half an"), started.set, HANG]
        made = new_task(tasks_api)
        tasks_api.post(f"/api/tasks/{made['id']}/run")
        assert started.wait(5)
        assert [t["running"] for t in tasks_of(tasks_api)] == [True]

    def test_a_second_run_while_one_is_running_is_refused(
        self, tasks_api: TestClient
    ) -> None:
        started = threading.Event()
        tasks_api.fakes["ollama"].script = [Text("Half an"), started.set, HANG]
        first, second = new_task(tasks_api, "One"), new_task(tasks_api, "Two")
        assert tasks_api.post(f"/api/tasks/{first['id']}/run").status_code == 202
        assert started.wait(5)

        response = tasks_api.post(f"/api/tasks/{second['id']}/run")

        assert error_of(response) == (429, "busy")
        assert response.json()["error"]["message"] == (
            "Another scheduled task is running. Try again when it finishes."
        )

    def test_a_model_that_is_gone_is_said_so_and_counts_for_nothing(
        self, tmp_path: Path, tz
    ) -> None:
        def seed(store: Store) -> None:
            seeded_task(store, model="ollama/not-installed:1b", next_run_at=TOMORROW)

        with tasks_app(tmp_path, tz, seed=seed) as client:
            (task,) = tasks_of(client)
            response = client.post(f"/api/tasks/{task['id']}/run")
            assert error_of(response) == (409, "cannot_run")
            assert response.json()["error"]["message"] == (
                "There is no model called ollama/not-installed:1b."
            )
            assert tasks_of(client)[0]["failures"] == 0

    def test_deleting_a_task_keeps_the_chats_it_made(
        self, tasks_api: TestClient
    ) -> None:
        made = new_task(tasks_api)
        chat_id = tasks_api.post(f"/api/tasks/{made['id']}/run").json()["chat_id"]
        wait_for(lambda: finished_task(tasks_api))

        assert tasks_api.delete(f"/api/tasks/{made['id']}").status_code == 204

        assert tasks_of(tasks_api) == []
        kept = tasks_api.get(f"/api/chats/{chat_id}").json()["chat"]
        assert kept["task_id"] is None

    def test_the_scheduler_runs_a_due_task_while_the_app_is_up(
        self, tmp_path: Path, tz
    ) -> None:
        def seed(store: Store) -> None:
            seeded_task(store, next_run_at=NOW - 3_600_000)  # an hour overdue

        with tasks_app(tmp_path, tz, seed=seed, tick=0.01) as client:
            done = wait_for(lambda: finished_task(client))
            assert done["last_status"] == "done" and done["next_run_at"] == TOMORROW

    def test_a_run_in_flight_when_the_app_stops_is_saved_as_stopped(
        self, tmp_path: Path, tz
    ) -> None:
        started = threading.Event()
        with tasks_app(tmp_path, tz) as client:
            client.fakes["ollama"].script = [Text("Half an"), started.set, HANG]
            made = new_task(client)
            chat_id = client.post(f"/api/tasks/{made['id']}/run").json()["chat_id"]
            assert started.wait(5)
        store = Store(tmp_path / "chats.db")
        reply = store.messages(chat_id)[-1]
        assert (reply.content, reply.status) == ("Half an", "stopped")
        store.close()

    def test_a_scheduled_run_in_flight_when_the_app_stops_is_saved_as_stopped(
        self, tmp_path: Path, tz
    ) -> None:
        started = threading.Event()

        def seed(store: Store) -> None:
            seeded_task(
                store, next_run_at=NOW + 60_000
            )  # not due until the clock moves

        with tasks_app(tmp_path, tz, seed=seed, tick=0.01) as client:
            client.fakes["ollama"].script = [Text("Half an"), started.set, HANG]
            client.clock.advance(120)
            assert started.wait(5)
        store = Store(tmp_path / "chats.db")
        (chat,) = store.list_chats()
        reply = store.messages(chat.id)[-1]
        assert (reply.content, reply.status) == ("Half an", "stopped")
        store.close()

    def test_an_unreadable_schedule_does_not_break_the_page(
        self, tmp_path: Path, tz
    ) -> None:
        def seed(store: Store) -> None:
            task = seeded_task(store, next_run_at=TOMORROW)
            store.update_task(task.id, {"schedule": {"kind": "fortnightly"}})

        with tasks_app(tmp_path, tz, seed=seed) as client:
            assert client.get("/api/bootstrap").status_code == 200
            (task,) = tasks_of(client)
            assert task["schedule_text"] == "Unreadable schedule"
