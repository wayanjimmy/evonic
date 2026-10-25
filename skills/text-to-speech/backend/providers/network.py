"""Safe, bounded outbound HTTP primitives for approved speech providers.

Provider adapters use fixed cloud hosts; agent tool arguments never reach this
code, so a synthesis request cannot become an arbitrary SSRF request.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .base import SafeErrorCode, SpeechGenerationError

_MAX_TIMEOUT_SECONDS = 300
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SafeEndpoint:
    """A fixed provider base URL and its request timeout."""

    base_url: str
    timeout_seconds: int


def _is_unroutable(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified))


def bounded_timeout(value: Any) -> int:
    """Validate a provider timeout against the shared outbound-request bound."""
    try:
        timeout = int(value if value is not None and value != "" else _DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as exc:
        raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider timeout must be an integer.") from exc
    if not 1 <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider timeout is outside the permitted range.")
    return timeout


def _upstream_message(exc: HTTPError) -> str:
    """Return a short, sanitized provider error message for 4xx responses.

    Lets the agent self-correct (bad model/voice) without exposing raw payloads.
    """
    try:
        body = json.loads(exc.read(4096))
        message = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        if message is None and "detail" in body:  # ElevenLabs: {"detail": {"message": ...}} or {"detail": "..."}
            detail = body["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
    except Exception:
        return ""
    if not isinstance(message, str):
        return ""
    return re.sub(r"[^\x20-\x7E]", "", message)[:200].strip()


class BoundedHttpClient:
    """No-redirect HTTPS client with response and timeout bounds."""

    def __init__(self, endpoint: SafeEndpoint) -> None:
        self._endpoint = endpoint
        self._opener = build_opener(HTTPRedirectHandler())
        self._opener.redirect_request = lambda *_args, **_kwargs: None

    def _url_for_path(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or "/../" in f"{path}/":
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider requested an unsafe endpoint path.")
        endpoint = urlparse(self._endpoint.base_url)
        if endpoint.scheme != "https":
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Cloud provider endpoints must use HTTPS.")
        url = f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path.rstrip('/')}{path}"
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(endpoint.hostname, endpoint.port or 443, type=socket.SOCK_STREAM)}
        except (OSError, ValueError) as exc:
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The provider host could not be resolved.") from exc
        if not addresses or any(_is_unroutable(address) for address in addresses):
            raise SpeechGenerationError(SafeErrorCode.PERMISSION_DENIED, "The provider endpoint is not publicly routable.")
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> tuple[bytes, str]:
        url = self._url_for_path(path)
        request = Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with self._opener.open(request, timeout=self._endpoint.timeout_seconds) as response:
                data = response.read(max_bytes + 1)
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise SpeechGenerationError(SafeErrorCode.PERMISSION_DENIED, "The provider rejected its configured credentials.") from exc
            if exc.code == 429:
                raise SpeechGenerationError(SafeErrorCode.RATE_LIMITED, "The provider is rate limiting speech requests.") from exc
            detail = _upstream_message(exc) if 400 <= exc.code < 500 else ""
            message = "The provider rejected the speech request."
            if detail:
                message = f"{message} Provider said: {detail}"
            elif exc.code >= 500:
                message = f"The provider failed to complete the speech request (HTTP {exc.code}); retry later."
            raise SpeechGenerationError(SafeErrorCode.GENERATION_FAILED, message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SpeechGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The provider could not be reached within the configured timeout.") from exc
        if len(data) > max_bytes:
            raise SpeechGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider response exceeded the permitted size.")
        return data, content_type

    def json(self, method: str, path: str, payload: Mapping[str, Any] | None = None, *, headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        data, _ = self.request(method, path, body=body, headers=request_headers)
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid response.") from exc
        if not isinstance(decoded, Mapping):
            raise SpeechGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid response.")
        return decoded
