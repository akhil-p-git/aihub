"""Request guards: known hosts only, an optional launch token, no cross-site writes, and strict headers.

The server listens on loopback, but a web page the browser has open can still reach it: with a forged
Origin (CSRF) or after DNS rebinding. Checking Host and Origin against `allowed_hosts` stops both.

Any other program on the machine can reach the port too. A launcher that must keep those out starts the
server with a random token (AIHUB_LAUNCH_TOKEN) and gives only its own window the matching cookie. The
systemd service sets no token, so it stays open to the local user as before.
"""

import hmac
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    # Remote images in model output would let a prompt-injected reply leak the chat to a URL.
    "img-src 'self'; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_COOKIE = "aihub_t"


def _hostname(value: str) -> str:
    try:
        return (urlsplit("//" + value).hostname or "").lower()
    except ValueError:
        return ""


def _origin_hostname(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower()  # "null" has none
    except ValueError:
        return ""


def _has_token(headers: Headers, token: bytes) -> bool:
    value = cookie_parser(headers.get("cookie", "")).get(TOKEN_COOKIE, "")
    # compare_digest raises on a str with non-ASCII characters, which would be a 500 instead of a 401.
    return hmac.compare_digest(value.encode("latin-1", "replace"), token)


class SecurityMiddleware:
    def __init__(
        self, app: ASGIApp, allowed_hosts: tuple[str, ...], token: bytes | None = None
    ) -> None:
        self.app = app
        self.allowed = frozenset(host.lower() for host in allowed_hosts)
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = self._with_headers(scope, send)
        refusal = self._refusal(scope, Headers(scope=scope))
        if refusal:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _refusal(self, scope: Scope, headers: Headers) -> Response | None:
        if _hostname(headers.get("host", "")) not in self.allowed:
            return PlainTextResponse(
                "Host not allowed. Add it to allowed_hosts in ~/.config/aihub/web.yaml.",
                status_code=400,
            )
        if (
            self.token is not None
            and scope["path"].startswith("/api/")
            and not _has_token(headers, self.token)
        ):
            # The same shape as the API's other errors, which the page reads.
            return JSONResponse(
                {"error": {"code": "not_authorised", "message": "Not authorised."}},
                status_code=401,
            )
        if scope["method"] not in UNSAFE_METHODS:
            return None
        if headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        origin = headers.get("origin")
        if origin is not None and _origin_hostname(origin) not in self.allowed:
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        has_body = (
            headers.get("content-length", "0") != "0" or "transfer-encoding" in headers
        )
        if has_body and not headers.get("content-type", "").startswith(
            "application/json"
        ):
            return PlainTextResponse("Send JSON.", status_code=415)
        return None

    @staticmethod
    def _with_headers(scope: Scope, send: Send) -> Send:
        is_api = scope["path"].startswith("/api/")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if is_api:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        return wrapped
