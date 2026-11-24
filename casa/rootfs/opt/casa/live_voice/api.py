"""The OpenAI live API: session control over REST, the control WebSocket, and
the Responses calls Casa makes beside a live session.

Session control lives under ``/v1/live/sessions/{sid}``: ``accept``,
``reject``, ``refer`` and ``hangup`` each answer 200 with an empty body. The
control channel is a WebSocket at ``.../{sid}/attach``. Decisions and records
are ordinary ``/v1/responses`` calls.

Every failure of a REST call is an :class:`ApiError`: ``status`` is the HTTP
status, or 0 for a timeout or a connection that failed; ``code`` is the
service's error code when it names one. :meth:`LiveApi.attach` is the
exception: it raises aiohttp's own errors (a refused upgrade is an
``aiohttp.WSServerHandshakeError``, a ``ClientError``). The API key is sent only in the Authorization header and
never appears in an error or a log line.
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp

DEFAULT_HOST = "api.openai.com"
ATTACH_HEARTBEAT_S = 20.0


class ApiError(Exception):
    """A live API call that failed; ``status`` is 0 when no answer arrived."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(message or f"{code} (status {status})")


class LiveApi:
    def __init__(self, *, key: str, host: str = "", http: aiohttp.ClientSession,
                 timeout_s: float = 10.0, base_url: str = "") -> None:
        # base_url exists for tests, which serve plain http on localhost.
        self._key = key
        self._http = http
        self._timeout_s = timeout_s
        self.base = base_url.rstrip("/") or f"https://{host or DEFAULT_HOST}"

    def __repr__(self) -> str:
        return f"LiveApi(base={self.base!r})"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def _session_url(self, sid: str, verb: str) -> str:
        return f"{self.base}/v1/live/sessions/{sid}/{verb}"

    async def _post(self, url: str, body: dict | None, timeout_s: float) -> bytes:
        """POST *body* as JSON (no body when None); return the raw 2xx answer."""
        try:
            async with self._http.post(
                    url, json=body, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                raw = await resp.read()
                if not 200 <= resp.status < 300:
                    raise ApiError(resp.status, _error_code(raw, resp.status))
                return raw
        except TimeoutError:
            raise ApiError(0, "timeout") from None
        except aiohttp.ClientError as exc:
            raise ApiError(0, "connection", type(exc).__name__) from None

    # Session control.

    async def accept(self, sid: str, session: dict) -> None:
        await self._post(self._session_url(sid, "accept"), {"session": session},
                         self._timeout_s)

    async def reject(self, sid: str, status_code: int = 486) -> None:
        await self._post(self._session_url(sid, "reject"),
                         {"status_code": status_code}, self._timeout_s)

    async def refer(self, sid: str, target_uri: str) -> None:
        await self._post(self._session_url(sid, "refer"),
                         {"target_uri": target_uri}, self._timeout_s)

    async def hangup(self, sid: str) -> None:
        await self._post(self._session_url(sid, "hangup"), None, self._timeout_s)

    def attach(self, sid: str):
        """The ``ws_connect`` context manager for *sid*'s control channel.

        It has no handshake timeout of its own: callers bound it (for example
        with ``asyncio.timeout``) and handle ``aiohttp.ClientError``."""
        url = self._session_url(sid, "attach")
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        return self._http.ws_connect(url, headers=self._headers(), max_msg_size=0,
                                     heartbeat=ATTACH_HEARTBEAT_S)

    # Responses calls.

    async def _responses(self, body: dict, timeout_s: float) -> dict:
        raw = await self._post(f"{self.base}/v1/responses", body, timeout_s)
        try:
            answer = json.loads(raw)
        except ValueError:
            raise ApiError(200, "invalid_output", "the answer is not JSON") from None
        if not isinstance(answer, dict):
            raise ApiError(200, "invalid_output", "the answer is not an object")
        return answer

    async def respond(self, *, model: str, instructions: str, input: str,
                      tools: list[dict], timeout_s: float) -> list[dict]:
        """The function calls the model made, as ``{"name", "arguments"}``."""
        answer = await self._responses(
            {"model": model, "instructions": instructions, "input": input,
             "tools": tools, "tool_choice": "auto"}, timeout_s)
        calls = []
        for item in _items(answer):
            if item.get("type") != "function_call":
                continue
            calls.append({"name": str(item.get("name") or ""),
                          "arguments": parse_arguments(item.get("arguments"))})
        return calls

    async def respond_json(self, *, model: str, instructions: str, input: str,
                           schema: dict, timeout_s: float) -> dict:
        """The object the model wrote under a strict JSON schema."""
        answer = await self._responses(
            {"model": model, "instructions": instructions, "input": input,
             "text": {"format": {"type": "json_schema", "name": "record",
                                 "schema": schema, "strict": True}}}, timeout_s)
        text = answer.get("output_text")
        if not isinstance(text, str):
            text = "".join(
                part.get("text", "")
                for item in _items(answer) if item.get("type") == "message"
                for part in item.get("content") or []
                if isinstance(part, dict) and isinstance(part.get("text"), str))
        try:
            value = json.loads(text)
        except ValueError:
            raise ApiError(200, "invalid_output", "the model did not write JSON") from None
        if not isinstance(value, dict):
            raise ApiError(200, "invalid_output", "the model did not write an object")
        return value


def _items(answer: dict) -> list[dict]:
    output = answer.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def parse_arguments(raw: Any) -> dict:
    """A function call's arguments: a JSON object string, else ``{}``."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(raw: bytes, status: int) -> str:
    """The service's error code from an error body, else ``http_<status>``."""
    try:
        error = json.loads(raw).get("error") or {}
        code = error.get("code") or error.get("type")
    except (ValueError, AttributeError):
        code = None
    return str(code) if code else f"http_{status}"
