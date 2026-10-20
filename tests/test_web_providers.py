import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from aihub_web.catalog import ModelInfo, claude_model, ollama_model, openrouter_model
from aihub_web.config import AnthropicConfig, Config, OllamaConfig, OpenRouterConfig
from aihub_web.providers import (
    Finish,
    GenRequest,
    ProviderError,
    Text,
    Thinking,
    Usage,
)
from aihub_web.providers import anthropic as anthropic_mod
from aihub_web.providers import ollama as ollama_mod
from aihub_web.providers import openrouter as openrouter_mod
from aihub_web.providers.base import error_from_body

FIXTURES = Path(__file__).parent / "fixtures"
OLLAMA = json.loads((FIXTURES / "ollama_models.json").read_text())
OPENROUTER = {
    r["id"]: r for r in json.loads((FIXTURES / "openrouter_models.json").read_text())
}
KEY = "sk-test-SECRET-1234"


def local(name: str) -> ModelInfo:
    tag = next(t for t in OLLAMA["tags"] if t["name"] == name)
    return ollama_model(tag, OLLAMA["show"][name], OllamaConfig())


def remote(model_id: str) -> ModelInfo:
    return openrouter_model(OPENROUTER[model_id], date(2026, 10, 20))


def claude(name: str) -> ModelInfo:
    return next(claude_model(m) for m in Config().anthropic.models if m.name == name)


def req(
    model: ModelInfo, effort: str | None = None, system: str = "", max_tokens: int = 256
):
    return GenRequest(
        model, [{"role": "user", "content": "hi"}], system, effort, max_tokens
    )


def client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def chunked(data: bytes, size: int):
    async def pieces():
        for start in range(0, len(data), size):
            yield data[start : start + size]

    return pieces()


async def collect(provider, request: GenRequest) -> list:
    return [event async for event in provider.stream(request)]


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (401, "auth"),
            (402, "quota"),
            (404, "model_not_found"),
            (429, "rate_limit"),
            (503, "routing"),
            (529, "overloaded"),
            (500, "upstream"),
            (418, "upstream"),
        ],
    )
    def test_status_codes(self, status: int, code: str) -> None:
        err = error_from_body(status, "{}", "Acme")
        assert (err.code, err.status) == (code, status)
        assert err.message.startswith("Acme") and str(status) in err.message

    def test_the_providers_own_words_are_kept(self) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": 429,
                    "message": "Rate limit exceeded: free-models-per-day",
                }
            }
        )
        assert "free-models-per-day" in error_from_body(429, body, "OpenRouter").message

    def test_spend_limits_are_quota_not_bad_requests(self) -> None:
        body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "You have reached your specified API usage limits.",
                },
            }
        )
        assert error_from_body(400, body, "Anthropic").code == "quota"
        assert (
            error_from_body(
                429, '{"error_code": "enforced_spend_limit_reached"}', "Anthropic"
            ).code
            == "quota"
        )

    def test_plain_text_and_huge_bodies_are_trimmed(self) -> None:
        err = error_from_body(502, "upstream  connect\nerror " * 100, "X")
        assert len(err.message) < 400 and "\n" not in err.message


class TestOllama:
    def test_think_is_sent_only_where_the_model_has_a_switch(self) -> None:
        cfg = OllamaConfig()
        with_switch, without = local("qwen3:8b"), local("llama3.2:1b")
        assert ollama_mod.request_body(req(with_switch, "off"), cfg)["think"] is False
        assert ollama_mod.request_body(req(with_switch, "on"), cfg)["think"] is True
        assert "think" not in ollama_mod.request_body(req(with_switch, None), cfg)
        assert "think" not in ollama_mod.request_body(req(without, "off"), cfg)

    def test_named_levels_are_sent_as_the_name(self) -> None:
        model = ollama_model(
            {"name": "gpt-oss:20b"},
            {
                "capabilities": ["completion", "thinking"],
                "thinking": {"values": ["low", "medium", "high"]},
            },
            OllamaConfig(),
        )
        assert (
            ollama_mod.request_body(req(model, "high"), OllamaConfig())["think"]
            == "high"
        )

    def test_context_is_constant_and_capped_by_the_model(self) -> None:
        cfg = OllamaConfig(num_ctx=16384)
        assert (
            ollama_mod.request_body(req(local("qwen3:8b")), cfg)["options"]["num_ctx"]
            == 16384
        )
        assert (
            ollama_mod.request_body(req(local("qwen2.5:3b")), cfg)["options"]["num_ctx"]
            == 16384
        )
        capped = ollama_mod.request_body(
            req(local("qwen2.5:3b")), OllamaConfig(num_ctx=65536)
        )
        assert capped["options"]["num_ctx"] == 32768

    def test_system_prompt_goes_first(self) -> None:
        body = ollama_mod.request_body(
            req(local("qwen3:8b"), system="Be brief."), OllamaConfig()
        )
        assert body["messages"][0] == {"role": "system", "content": "Be brief."}
        assert body["messages"][1]["role"] == "user" and body["stream"] is True

    @pytest.mark.anyio
    async def test_real_stream_with_thinking(self) -> None:
        data = (FIXTURES / "ollama_stream_thinking.ndjson").read_bytes()
        seen = {}

        def handle(request: httpx2.Request) -> httpx2.Response:
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, content=chunked(data, 7))

        provider = ollama_mod.OllamaProvider(OllamaConfig(), client(handle))
        events = await collect(provider, req(local("qwen3:8b"), "on"))
        thinking = "".join(e.text for e in events if isinstance(e, Thinking))
        assert thinking.startswith("Okay, the user") and not any(
            isinstance(e, Text) for e in events
        )
        usage = next(e for e in events if isinstance(e, Usage))
        assert (usage.input_tokens, usage.output_tokens) == (13, 60)
        assert usage.tokens_per_second == pytest.approx(60 / 0.864756)
        assert events[-1] == Finish("length")
        assert seen["body"]["think"] is True and seen["body"]["model"] == "qwen3:8b"

    @pytest.mark.anyio
    async def test_real_plain_stream(self) -> None:
        data = (FIXTURES / "ollama_stream_plain.ndjson").read_bytes()
        provider = ollama_mod.OllamaProvider(
            OllamaConfig(), client(lambda r: httpx2.Response(200, content=data))
        )
        events = await collect(provider, req(local("llama3.2:1b")))
        assert "".join(e.text for e in events if isinstance(e, Text)).strip() != ""
        assert not any(isinstance(e, Thinking) for e in events)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("status", "body", "code"),
        [
            (404, '{"error":"model \'x\' not found"}', "model_not_found"),
            (400, '{"error":"\\"x\\" does not support thinking"}', "bad_request"),
        ],
    )
    async def test_http_errors(self, status: int, body: str, code: str) -> None:
        provider = ollama_mod.OllamaProvider(
            OllamaConfig(), client(lambda r: httpx2.Response(status, text=body))
        )
        with pytest.raises(ProviderError) as err:
            await collect(provider, req(local("qwen3:8b")))
        assert err.value.code == code and "x" in err.value.message

    @pytest.mark.anyio
    async def test_error_line_mid_stream(self) -> None:
        data = b'{"message":{"content":"par"},"done":false}\n{"error":"llama runner process has terminated"}\n'
        provider = ollama_mod.OllamaProvider(
            OllamaConfig(), client(lambda r: httpx2.Response(200, content=data))
        )
        seen = []
        with pytest.raises(ProviderError, match="terminated"):
            async for event in provider.stream(req(local("qwen3:8b"))):
                seen.append(event)
        assert seen == [Text("par")]

    @pytest.mark.anyio
    async def test_ollama_not_running_says_how_to_start_it(self) -> None:
        def refuse(request: httpx2.Request):
            raise httpx2.ConnectError("refused", request=request)

        provider = ollama_mod.OllamaProvider(OllamaConfig(), client(refuse))
        with pytest.raises(ProviderError) as err:
            await collect(provider, req(local("qwen3:8b")))
        assert (
            err.value.code == "unreachable"
            and "systemctl --user start ollama" in err.value.message
        )


OPENROUTER_STREAM = (
    ": OPENROUTER PROCESSING\n\n"
    'data: {"id":"gen-1","choices":[{"index":0,"delta":{"role":"assistant","content":"","reasoning":"Greet back. "}}]}\n\n'
    ": OPENROUTER PROCESSING\n\n"
    'data: {"id":"gen-1","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
    'data: {"id":"gen-1","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"id":"gen-1","choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":12,"completion_tokens":30,"total_tokens":42,"cost":0.0000123}}\n\n'
    "data: [DONE]\n\n"
)


class TestOpenRouter:
    def provider(self, handler, environ=None):
        env = {"AIHUB_OPENROUTER_KEY": KEY} if environ is None else environ
        return openrouter_mod.OpenRouterProvider(
            OpenRouterConfig(), client(handler), env
        )

    def test_reasoning_effort_mapping(self) -> None:
        cfg = OpenRouterConfig()
        model = remote("deepseek/deepseek-v4.1-flash-1017")
        assert openrouter_mod.request_body(req(model, "off"), cfg)["reasoning"] == {
            "effort": "none"
        }
        assert openrouter_mod.request_body(req(model, "high"), cfg)["reasoning"] == {
            "effort": "high"
        }
        assert "reasoning" not in openrouter_mod.request_body(req(model, None), cfg)
        no_switch = remote("google/gemma-4-31b-it:free")
        assert "reasoning" not in openrouter_mod.request_body(
            req(no_switch, "high"), cfg
        )

    def test_privacy_routing_applies_to_paid_models_only(self) -> None:
        cfg = OpenRouterConfig()
        paid = openrouter_mod.request_body(
            req(remote("deepseek/deepseek-v4.1-flash-1017")), cfg
        )
        assert paid["provider"]["data_collection"] == "deny"
        free = openrouter_mod.request_body(
            req(remote("google/gemma-4-31b-it:free")), cfg
        )
        assert "provider" not in free

    def test_usage_options_are_not_sent_because_openrouter_ignores_them(self) -> None:
        body = openrouter_mod.request_body(
            req(remote("google/gemma-4-31b-it:free")), OpenRouterConfig()
        )
        assert "usage" not in body and "stream_options" not in body

    @pytest.mark.anyio
    async def test_stream_with_comments_reasoning_and_usage(self) -> None:
        seen = {}

        def handle(request: httpx2.Request) -> httpx2.Response:
            seen["auth"] = request.headers["authorization"]
            seen["url"] = str(request.url)
            return httpx2.Response(200, content=chunked(OPENROUTER_STREAM.encode(), 11))

        events = await collect(
            self.provider(handle), req(remote("google/gemma-4-31b-it:free"))
        )
        assert events == [
            Thinking("Greet back. "),
            Text("Hel"),
            Text("lo"),
            Finish("stop"),
            Usage(12, 30, Decimal("0.0000123")),
        ]
        assert seen["auth"] == f"Bearer {KEY}" and seen["url"].endswith(
            "/api/v1/chat/completions"
        )

    def test_reasoning_details_are_used_when_there_is_no_plain_reasoning(self) -> None:
        chunk = {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "step one"},
                            {"type": "reasoning.encrypted", "data": "xxxx"},
                            {"type": "reasoning.summary", "summary": " and two"},
                        ]
                    }
                }
            ]
        }
        assert openrouter_mod.parse_chunk(chunk) == [Thinking("step one and two")]

    def test_reasoning_is_not_counted_twice(self) -> None:
        chunk = {
            "choices": [
                {"delta": {"reasoning": "abc", "reasoning_details": [{"text": "abc"}]}}
            ]
        }
        assert openrouter_mod.parse_chunk(chunk) == [Thinking("abc")]

    def test_error_after_the_stream_started(self) -> None:
        chunk = {
            "id": "gen-1",
            "error": {
                "code": "server_error",
                "message": "Provider disconnected unexpectedly",
            },
            "choices": [
                {"index": 0, "delta": {"content": ""}, "finish_reason": "error"}
            ],
        }
        with pytest.raises(ProviderError, match="Provider disconnected") as err:
            openrouter_mod.parse_chunk(chunk)
        assert err.value.code == "upstream"

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("status", "code"),
        [(401, "auth"), (402, "quota"), (429, "rate_limit"), (503, "routing")],
    )
    async def test_http_errors_never_echo_the_key(self, status: int, code: str) -> None:
        body = json.dumps({"error": {"code": status, "message": "nope"}})
        provider = self.provider(lambda r: httpx2.Response(status, text=body))
        with pytest.raises(ProviderError) as err:
            await collect(provider, req(remote("google/gemma-4-31b-it:free")))
        assert err.value.code == code and "nope" in err.value.message
        assert KEY not in err.value.message and KEY not in repr(err.value)

    @pytest.mark.anyio
    async def test_no_key_means_no_request(self) -> None:
        def explode(request: httpx2.Request):
            raise AssertionError("must not call the network without a key")

        with pytest.raises(ProviderError) as err:
            await collect(
                self.provider(explode, environ={"AIHUB_OPENROUTER_KEY": "  "}),
                req(remote("google/gemma-4-31b-it:free")),
            )
        assert err.value.code == "no_key"

    @pytest.mark.anyio
    async def test_a_cut_off_stream_just_ends(self) -> None:
        partial = 'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
        events = await collect(
            self.provider(lambda r: httpx2.Response(200, text=partial)),
            req(remote("google/gemma-4-31b-it:free")),
        )
        assert events == [Text("par")]


def sse(*pairs: tuple[str, dict]) -> str:
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in pairs
    )


def delta(index: int, kind: str, key: str, value: str) -> tuple[str, dict]:
    return "content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": kind, key: value},
    }


ANTHROPIC_STREAM = sse(
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 25, "output_tokens": 1},
            },
        },
    ),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
    ),
    ("ping", {"type": "ping"}),
    delta(0, "thinking_delta", "thinking", "I should greet."),
    delta(0, "signature_delta", "signature", "EqQBCgIYAhIM"),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    delta(1, "text_delta", "text", "Hello"),
    delta(1, "text_delta", "text", "!"),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 15},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
)


class TestAnthropic:
    def provider(self, handler, environ=None):
        env = {"AIHUB_ANTHROPIC_KEY": KEY} if environ is None else environ
        return anthropic_mod.AnthropicProvider(AnthropicConfig(), client(handler), env)

    def test_thinking_is_summarised_so_it_can_be_shown(self) -> None:
        body = anthropic_mod.request_body(
            req(claude("Opus 5.5"), "high"), AnthropicConfig()
        )
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert body["output_config"] == {"effort": "high"}
        assert body["stream"] is True and body["model"] == "claude-opus-5-5"

    def test_off_uses_the_mode_each_model_accepts(self) -> None:
        cfg = AnthropicConfig()
        haiku = anthropic_mod.request_body(req(claude("Haiku 5.5"), "off"), cfg)
        sonnet = anthropic_mod.request_body(req(claude("Sonnet 5.5"), "off"), cfg)
        assert (
            haiku["thinking"] == {"type": "disabled"} and "output_config" not in haiku
        )
        assert (
            sonnet["thinking"] == {"type": "between_tools"}
            and "output_config" not in sonnet
        )

    def test_a_model_that_cannot_switch_off_never_gets_a_disable_request(self) -> None:
        body = anthropic_mod.request_body(
            req(claude("Opus 5.5"), "off"), AnthropicConfig()
        )
        assert "thinking" not in body

    def test_system_prompt_is_a_top_level_field(self) -> None:
        body = anthropic_mod.request_body(
            req(claude("Haiku 5.5"), "low", system="Be brief."), AnthropicConfig()
        )
        assert body["system"] == "Be brief."
        assert all(m["role"] != "system" for m in body["messages"])

    @pytest.mark.anyio
    async def test_stream(self) -> None:
        seen = {}

        def handle(request: httpx2.Request) -> httpx2.Response:
            seen["headers"] = dict(request.headers)
            seen["url"] = str(request.url)
            return httpx2.Response(200, content=chunked(ANTHROPIC_STREAM.encode(), 13))

        events = await collect(self.provider(handle), req(claude("Haiku 5.5"), "low"))
        assert events == [
            Thinking("I should greet."),
            Text("Hello"),
            Text("!"),
            Finish("end_turn"),
            Usage(25, 15),
        ]
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert (
            seen["headers"]["x-api-key"] == KEY
            and seen["headers"]["anthropic-version"] == "2023-06-01"
        )

    def test_omitted_thinking_streams_nothing(self) -> None:
        parser = anthropic_mod.StreamParser()
        event = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": ""},
        }
        assert parser.feed(event) == []

    def test_cached_input_counts_toward_input_tokens(self) -> None:
        parser = anthropic_mod.StreamParser()
        parser.feed(
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 25,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 0,
                    }
                },
            }
        )
        parser.feed(
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 7}}
        )
        assert parser.feed({"type": "message_stop"}) == [Usage(125, 7)]

    def test_final_usage_in_message_delta_wins(self) -> None:
        parser = anthropic_mod.StreamParser()
        parser.feed(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 1}},
            }
        )
        parser.feed(
            {
                "type": "message_delta",
                "delta": {},
                "usage": {"input_tokens": 10682, "output_tokens": 510},
            }
        )
        assert parser.feed({"type": "message_stop"}) == [Usage(10682, 510)]

    def test_error_event_in_the_stream(self) -> None:
        parser = anthropic_mod.StreamParser()
        with pytest.raises(ProviderError) as err:
            parser.feed(
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "Overloaded"},
                }
            )
        assert err.value.code == "overloaded" and "Overloaded" in err.value.message

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("status", "error_type", "message", "code"),
        [
            (
                400,
                "invalid_request_error",
                "You have reached your specified API usage limits.",
                "quota",
            ),
            (400, "invalid_request_error", "max_tokens: too large", "bad_request"),
            (401, "authentication_error", "invalid x-api-key", "auth"),
            (404, "not_found_error", "model: claude-x", "model_not_found"),
            (429, "rate_limit_error", "Number of requests exceeded", "rate_limit"),
            (529, "overloaded_error", "Overloaded", "overloaded"),
        ],
    )
    async def test_http_errors(
        self, status: int, error_type: str, message: str, code: str
    ) -> None:
        body = json.dumps(
            {"type": "error", "error": {"type": error_type, "message": message}}
        )
        provider = self.provider(lambda r: httpx2.Response(status, text=body))
        with pytest.raises(ProviderError) as err:
            await collect(provider, req(claude("Haiku 5.5")))
        assert err.value.code == code and message in err.value.message
        assert KEY not in err.value.message

    @pytest.mark.anyio
    async def test_no_key_means_no_request(self) -> None:
        def explode(request: httpx2.Request):
            raise AssertionError("must not call the network without a key")

        with pytest.raises(ProviderError) as err:
            await collect(self.provider(explode, environ={}), req(claude("Haiku 5.5")))
        assert err.value.code == "no_key"
