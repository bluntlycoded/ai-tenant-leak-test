"""Endpoint connector.

One job: send a prompt to the target chat endpoint as a named tenant's
low-privilege user, and pull the answer, citations, and visible metadata out of
whatever JSON shape comes back.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Config, dig, bury
from .models import Observation


class ConnectorError(Exception):
    pass


class Connector:
    def __init__(self, config: Config):
        self.config = config
        self._client = httpx.Client(timeout=config.endpoint.timeout_seconds)

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

        try:
            payload = response.json()
        except ValueError:
            # Not JSON — still usable. Treat the whole body as the answer so a
            # plain-text endpoint does not silently test nothing.
            return Observation(
                answer=response.text,
                http_status=response.status_code,
                raw=None,
                schema_found={"answer": bool(response.text), "citations": False, "metadata": False},
                attempts=attempts,
            )

        answer = dig(payload, ep.response_text_field)
        citations = dig(payload, ep.citations_field) if ep.citations_field else None
        metadata = dig(payload, ep.metadata_field) if ep.metadata_field else None

        if answer is None and response.status_code >= 400:
            raise ConnectorError(f"endpoint returned HTTP {response.status_code}: {response.text[:300]}")

        return Observation(
            answer=answer if isinstance(answer, str) else ("" if answer is None else str(answer)),
            citations=_as_list(citations),
            metadata=metadata if isinstance(metadata, dict) else ({} if metadata is None else {"value": metadata}),
            http_status=response.status_code,
            raw=payload if isinstance(payload, dict) else {"value": payload},
            schema_found={
                "answer": answer is not None,
                # Only meaningful when the path is configured at all.
                "citations": citations is not None if ep.citations_field else False,
                "metadata": metadata is not None if ep.metadata_field else False,
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
        attempts = ep.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(ep.method, ep.url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 500 or attempt == attempts:
                    return response, attempt
                last_error = ConnectorError(f"HTTP {response.status_code}")

            if attempt < attempts:
                time.sleep(ep.retry_backoff_seconds * attempt)

        raise ConnectorError(
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
