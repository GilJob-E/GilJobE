from __future__ import annotations

import json
from collections.abc import Iterable, Iterator


def iter_sse_delta_text(lines: Iterable[bytes]) -> Iterator[str]:
    """Yield text deltas from OpenAI/vLLM SSE byte lines.

    Malformed/heartbeat lines are ignored so one bad event does not tear down a
    client stream.
    """
    for raw in lines:
        if not raw or not raw.startswith(b"data: "):
            continue
        body = raw[6:]
        if body == b"[DONE]":
            break
        try:
            chunk = json.loads(body.decode("utf-8"))
            content = chunk["choices"][0].get("delta", {}).get("content")
        except Exception:
            continue
        if content is not None:
            yield content
