"""Endpoint connector.

One job: send a prompt to the target chat endpoint as a named tenant's
low-privilege user, and pull the answer, citations, and visible metadata out of
whatever JSON shape comes back.
"""

from __future__ import annotations

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

        try:
            response = self._client.request(ep.method, ep.url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"request to {ep.url} failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            # Not JSON — still usable. Treat the whole body as the answer so a
            # plain-text endpoint does not silently test nothing.
            return Observation(
                answer=response.text,
                http_status=response.status_code,
                raw=None,
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
        )


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
