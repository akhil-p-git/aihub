"""Request guards: known hosts only, no cross-site writes, and strict headers on every response.

The server listens on loopback, but a web page the browser has open can still reach it: with a forged
Origin (CSRF) or after DNS rebinding. Checking Host and Origin against `allowed_hosts` stops both.
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
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


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = frozenset(host.lower() for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = self._with_headers(scope, send)
        refusal = self._refusal(scope["method"], Headers(scope=scope))
        if refusal:
            status, text = refusal
            await PlainTextResponse(text, status_code=status)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _refusal(self, method: str, headers: Headers) -> tuple[int, str] | None:
        if _hostname(headers.get("host", "")) not in self.allowed:
            return (
                400,
                "Host not allowed. Add it to allowed_hosts in ~/.config/aihub/web.yaml.",
            )
        if method not in UNSAFE_METHODS:
            return None
        if headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return 403, "Cross-site request refused."
        origin = headers.get("origin")
        if origin is not None and _origin_hostname(origin) not in self.allowed:
            return 403, "Cross-site request refused."
        has_body = (
            headers.get("content-length", "0") != "0" or "transfer-encoding" in headers
        )
        if has_body and not headers.get("content-type", "").startswith(
            "application/json"
        ):
            return 415, "Send JSON."
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
