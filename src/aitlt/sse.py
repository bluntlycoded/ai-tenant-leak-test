"""Server-sent events parsing and aggregation.

Narrow by design. This exists to turn a streamed chat response back into the
one shape the rest of the tool understands — an answer string plus optional
citations and metadata — and nothing more. No incremental consumption, no
reconnection, no `Last-Event-ID`: the client buffers the whole stream and then
reassembles it, which is right for a test harness talking to staging and wrong
for a production client.

The payload shapes inside `data:` frames vary by vendor, so the text delta is
located by a configurable dotted path rather than guessed at. Citations and
metadata reuse the same dotted paths as the non-streaming contract, resolved
against whichever frame carries them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import dig


@dataclass
class Aggregated:
    """A stream folded back into the non-streaming contract."""

    answer: str = ""
    #: The frame that last resolved the citations path, if any.
    citations: Any = None
    #: The frame that last resolved the metadata path, if any.
    metadata: Any = None
    frames: int = 0
    #: True when at least one frame contributed answer text.
    answer_found: bool = False
    #: Frames that were not valid JSON. Recorded rather than hidden — a stream
    #: that is mostly unparseable should not look like a short answer.
    unparseable_frames: int = 0
    raw_frames: list[dict[str, Any]] = field(default_factory=list)


def iter_frames(body: str) -> Iterator[str]:
    """Yield the `data` payload of each SSE event, in order.

    Follows the parts of the SSE grammar that matter here: events are separated
    by blank lines, `data:` lines within one event are joined with newlines, and
    comments (`:` prefix) plus `event:`/`id:`/`retry:` fields are ignored.
    """
    data_lines: list[str] = []
    for raw_line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if raw_line.startswith(":"):  # comment / keep-alive
            continue
        field_name, _, value = raw_line.partition(":")
        if field_name != "data":
            continue
        data_lines.append(value[1:] if value.startswith(" ") else value)

    if data_lines:  # stream ended without a trailing blank line
        yield "\n".join(data_lines)


def aggregate(
    body: str,
    *,
    delta_field: str,
    done_sentinel: str = "[DONE]",
    text_field: str | None = None,
    citations_field: str | None = None,
    metadata_field: str | None = None,
) -> Aggregated:
    """Fold an SSE body into one answer plus the last-seen side channels.

    `delta_field` is appended across frames. `text_field`, when it resolves to a
    non-empty string, is treated as a complete answer from that frame — some
    APIs send incremental deltas and then a final whole message. Deltas win when
    both are present, because a truncated stream should read as truncated rather
    than silently repaired by a final frame that may not arrive.
    """
    out = Aggregated()
    deltas: list[str] = []
    whole_messages: list[str] = []

    for payload in iter_frames(body):
        if payload.strip() == done_sentinel:
            break
        out.frames += 1

        try:
            frame = json.loads(payload)
        except ValueError:
            out.unparseable_frames += 1
            continue
        if not isinstance(frame, dict):
            out.unparseable_frames += 1
            continue

        out.raw_frames.append(frame)

        delta = dig(frame, delta_field)
        if isinstance(delta, str) and delta:
            deltas.append(delta)

        if text_field:
            whole = dig(frame, text_field)
            if isinstance(whole, str) and whole:
                whole_messages.append(whole)

        if citations_field:
            found = dig(frame, citations_field)
            if found is not None:
                out.citations = found
        if metadata_field:
            found = dig(frame, metadata_field)
            if found is not None:
                out.metadata = found

    if deltas:
        out.answer = "".join(deltas)
        out.answer_found = True
    elif whole_messages:
        out.answer = whole_messages[-1]
        out.answer_found = True

    return out
