"""Ingest verification stamp.

`verify-ingest` proves the fixtures are actually retrievable and the response
contract resolves. A later `test` run has no way to know that happened, so the
verification writes a stamp and the run reads it.

The stamp records a hash of the fixtures. If they are regenerated afterwards,
the canaries in staging no longer match the ones being searched for, every test
passes vacuously, and the stamp is stale — which the report says out loud
instead of quietly reporting a clean run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

STAMP_NAME = ".ingest-verified.json"


def fixtures_digest(fixtures_path: str | Path) -> str:
    return hashlib.sha256(Path(fixtures_path).read_bytes()).hexdigest()[:16]


def _stamp_path(fixtures_path: str | Path) -> Path:
    return Path(fixtures_path).parent / STAMP_NAME


def write(fixtures_path: str | Path, endpoint: str) -> Path:
    path = _stamp_path(fixtures_path)
    path.write_text(
        json.dumps(
            {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "fixtures_digest": fixtures_digest(fixtures_path),
                "endpoint": endpoint,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def read(fixtures_path: str | Path, endpoint: str) -> tuple[bool, datetime | None, str]:
    """Return (valid, verified_at, reason).

    Valid only when the stamp exists, the fixtures are unchanged since it was
    written, and it was taken against the same endpoint.
    """
    path = _stamp_path(fixtures_path)
    if not path.exists():
        return False, None, "verify-ingest has not been run for these fixtures"

    try:
        data = json.loads(path.read_text())
        verified_at = datetime.fromisoformat(data["verified_at"])
    except (ValueError, KeyError):
        return False, None, "the ingest stamp is unreadable"

    if data.get("fixtures_digest") != fixtures_digest(fixtures_path):
        return False, verified_at, "fixtures changed since verification — canaries in staging no longer match"
    if data.get("endpoint") != endpoint:
        return False, verified_at, f"verified against a different endpoint ({data.get('endpoint')})"
    return True, verified_at, "verified"
