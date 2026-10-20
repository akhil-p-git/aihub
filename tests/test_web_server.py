import contextlib
import hmac
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest
import uvicorn
from aihub_web.chat import ChatRequest
from aihub_web.config import Config
from aihub_web.db import Store
from aihub_web.providers import Finish, ProviderError, Text, Thinking, Usage
from aihub_web.security import CSP, SecurityMiddleware
from aihub_web.server import create_app, ndjson
from helpers import HANG, KEYS, TODAY, Clock, FakeProvider, handler_for, make_service
from starlette.testclient import TestClient
from test_web_shutdown import free_port

BASE = "http://127.0.0.1:8787"
FREE = "openrouter/google/gemma-4-31b-it:free"
LOCAL = "ollama/qwen3:8b"
HAIKU = "anthropic/claude-haiku-5-5"
# Shaped like the desktop shell's token: 32 random bytes as 43 base64url characters.
TOKEN = secrets.token_urlsafe(32)


def build(tmp_path: Path, environ=KEYS, **config):
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

    def test_every_api_path_and_method_needs_it(self, guarded: TestClient) -> None:
        for method, path in [
            ("POST", "/api/chat"),
            ("GET", "/api/chats"),
            ("PUT", "/api/settings"),
            ("DELETE", "/api/chats"),
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
