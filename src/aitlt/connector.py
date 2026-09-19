"""Endpoint connector.

One job: send a prompt to the target chat endpoint as a named tenant's
low-privilege user, and pull the answer, citations, and visible metadata out of
whatever JSON shape comes back.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import sse
from .config import Config, dig, bury
from .models import Observation


class ConnectorError(Exception):
    """Base for every reason a request could not produce a usable observation.

    All of these mark the test as `error`, which makes the run incomplete. That
    is deliberate: a misfit endpoint must fail loudly and fast rather than
    yielding a confusing half-success that reads as a clean report.
    """


class AuthError(ConnectorError):
    """401/403. Never retried — a wrong token will still be wrong in 500ms."""


class ConnectorTimeout(ConnectorError):
    """Transport failure or timeout that survived every retry."""


class HttpStatusError(ConnectorError):
    """Non-2xx that is not an auth failure."""


class MalformedResponseError(ConnectorError):
    """Response body could not be parsed as the configured contract."""


class UnsupportedResponseError(ConnectorError):
    """A response shape V1 deliberately does not handle, such as SSE.

    Named rather than muddled through. A streamed body would otherwise fall to
    the plain-text branch and be scanned as one blob: canaries might be found,
    citations and metadata never would, and the run would look like a partial
    success. Refusing is honest; guessing is the failure mode this tool exists
    to eliminate.
    """


class UnresolvedPathError(MalformedResponseError):
    """The required answer path is absent from the response.

    Distinct from an empty answer: absent means the configured contract does not
    describe this endpoint, and every subsequent test would scan nothing.
    """


#: Response content types V1 refuses rather than guesses at. Detected on the
#: response, not the config, because an endpoint may stream regardless of what
#: the operator believed it did.
UNSUPPORTED_CONTENT_TYPES = ("text/event-stream", "application/x-ndjson", "application/jsonl")


class Connector:
    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._client = httpx.Client(
            timeout=config.endpoint.timeout_seconds,
            transport=transport,
        )
        #: Set once any request is accepted, so a later 401 can be reported as
        #: an expiring credential rather than a wrong one.
        self._authenticated_ok = False

    def __enter__(self) -> "Connector":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def ask(self, tenant_id: str, prompt: str) -> Observation:
        ep = self.config.endpoint
        auth = self.config.tenants.get(tenant_id)
        if auth is None:
            raise ConnectorError(
                f"no auth configured for tenant {tenant_id!r}; "
                f"known tenants: {sorted(self.config.tenants)}"
            )

        headers = {**ep.headers, **auth.headers}
        body: dict[str, Any] = {**ep.body, **auth.body}
        bury(body, ep.prompt_field, prompt)

        response, attempts = self._send(ep, headers, body)

        if response.status_code in (401, 403):
            if self._authenticated_ok:
                # Earlier requests on this run were accepted, so the credential
                # was valid and stopped being valid. V1 has no refresh flow.
                raise AuthError(
                    f"HTTP {response.status_code} for tenant {tenant_id!r} after earlier requests "
                    f"succeeded. The credential most likely expired mid-run. V1 supports static "
                    f"tokens only — issue a longer-lived test credential rather than one that "
                    f"needs refreshing."
                )
            raise AuthError(
                f"HTTP {response.status_code} for tenant {tenant_id!r} on the first attempt. "
                f"The test user's credential was rejected — check the token behind this "
                f"tenant's config."
            )
        if response.status_code >= 400:
            raise HttpStatusError(
                f"endpoint returned HTTP {response.status_code}: {response.text[:300]}"
            )
        self._authenticated_ok = True

        content_type = response.headers.get("content-type", "").lower()
        is_sse = "text/event-stream" in content_type

        if is_sse and ep.streaming.mode == "sse":
            return self._observe_sse(ep, response, attempts)

        if any(marker in content_type for marker in UNSUPPORTED_CONTENT_TYPES):
            hint = (
                "Set `endpoint.streaming.mode: sse` and point `delta_field` at the text chunk "
                "inside each frame."
                if is_sse
                else "Point the config at a non-streaming JSON mode if the API has one "
                '(often `"stream": false` in the request body).'
            )
            raise UnsupportedResponseError(
                f"endpoint returned `{content_type.split(';')[0]}`, which is not handled by the "
                f"current configuration. {hint}"
            )

        try:
            payload = response.json()
        except ValueError:
            # Not JSON — still usable. Treat the whole body as the answer so a
            # plain-text endpoint does not silently test nothing.
            if not response.text.strip():
                raise MalformedResponseError(
                    "response was neither JSON nor text; nothing to scan"
                ) from None
            return Observation(
                answer=response.text,
                http_status=response.status_code,
                raw=None,
                schema_found={"answer": True, "citations": False, "metadata": False},
                attempts=attempts,
            )

        if not isinstance(payload, dict):
            raise MalformedResponseError(
                f"expected a JSON object at the top level, got {type(payload).__name__}"
            )

        answer = dig(payload, ep.response_text_field)
        citations = dig(payload, ep.citations_field) if ep.citations_field else None
        metadata = dig(payload, ep.metadata_field) if ep.metadata_field else None

        # The answer path is the one part of the contract that cannot be
        # optional. If it is absent, the configured contract does not describe
        # this endpoint and every test would scan an empty string.
        if answer is None:
            raise UnresolvedPathError(
                f"response_text_field `{ep.response_text_field}` did not resolve. "
                f"Top-level keys present: {sorted(payload)[:10]}. "
                f"Fix the dotted path in your config."
            )

        return Observation(
            answer=answer if isinstance(answer, str) else ("" if answer is None else str(answer)),
            citations=_as_list(citations),
            metadata=metadata if isinstance(metadata, dict) else ({} if metadata is None else {"value": metadata}),
            http_status=response.status_code,
            raw=payload if isinstance(payload, dict) else {"value": payload},
            schema_found={
                "answer": True,
                # Only meaningful when the path is configured at all.
                "citations": citations is not None if ep.citations_field else False,
                "metadata": metadata is not None if ep.metadata_field else False,
            },
            attempts=attempts,
        )

    def _observe_sse(self, ep, response: httpx.Response, attempts: int) -> Observation:
        """Fold a streamed response back into the ordinary contract.

        Everything downstream is unchanged: a canary in a streamed answer is the
        same canary, so detection, severity and reporting do not need to know
        the transport differed.
        """
        agg = sse.aggregate(
            response.text,
            delta_field=ep.streaming.delta_field,
            done_sentinel=ep.streaming.done_sentinel or "\0",
            text_field=ep.response_text_field,
            citations_field=ep.citations_field,
            metadata_field=ep.metadata_field,
        )

        if agg.frames == 0:
            raise MalformedResponseError(
                "streamed response contained no `data:` frames; nothing to scan"
            )
        if not agg.answer_found:
            raise UnresolvedPathError(
                f"no frame in the stream resolved `{ep.streaming.delta_field}` "
                f"(or `{ep.response_text_field}`). {agg.frames} frame(s) received, "
                f"{agg.unparseable_frames} unparseable. Keys seen in the first frame: "
                f"{sorted(agg.raw_frames[0])[:10] if agg.raw_frames else 'none'}. "
                f"Fix `endpoint.streaming.delta_field`."
            )

        return Observation(
            answer=agg.answer,
            citations=_as_list(agg.citations),
            metadata=agg.metadata if isinstance(agg.metadata, dict) else ({} if agg.metadata is None else {"value": agg.metadata}),
            http_status=response.status_code,
            raw={"sse_frames": agg.raw_frames},
            schema_found={
                "answer": True,
                "citations": agg.citations is not None if ep.citations_field else False,
                "metadata": agg.metadata is not None if ep.metadata_field else False,
            },
            attempts=attempts,
        )

    def _send(self, ep, headers: dict[str, str], body: dict[str, Any]) -> tuple[httpx.Response, int]:
        """Send with bounded retries on transport errors and 5xx.

        Staging endpoints are flaky, and a transient 502 that turns into an
        "error" result reports a boundary as untested when it is probably fine.
        Only transport failures and server errors are retried — a successful
        response is never re-sent, which keeps the cache tests honest.
        """
        last_error: Exception | None = None
        attempts = max(1, ep.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(ep.method, ep.url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                # 4xx is the caller's fault and will not improve on retry.
                if response.status_code < 500 or attempt == attempts:
                    return response, attempt
                last_error = HttpStatusError(f"HTTP {response.status_code}")

            if attempt < attempts:
                time.sleep(ep.retry_backoff_seconds * attempt)

        raise ConnectorTimeout(
            f"request to {ep.url} failed after {attempts} attempt(s): {last_error}"
        ) from last_error


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                # Citation objects: keep the whole thing, since a leaked source id
                # or title can hide in any field.
                out.append(", ".join(f"{k}={v}" for k, v in item.items()))
            else:
                out.append(str(item))
        return out
    return [str(value)]
