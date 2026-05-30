from __future__ import annotations

import os
from typing import Iterable

import requests

from .vllm_stream import iter_sse_delta_text


class VllmClient:
    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.base_url = base_url.rstrip("/")

    def health(self) -> dict:
        r = requests.get(f"{self.base_url}/v1/models", timeout=5)
        r.raise_for_status()
        return r.json()

    def stream_chat(self, payload: dict) -> Iterable[str]:
        r = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=300,
        )
        r.raise_for_status()
        yield from iter_sse_delta_text(r.iter_lines())

    def chat(self, payload: dict) -> dict:
        """non-stream chat → 전체 응답 JSON(choices/usage 포함). 구조화 출력 파싱용."""
        r = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json={**payload, "stream": False},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()


def default_vllm_client() -> VllmClient:
    return VllmClient(os.getenv("VLLM_BASE_URL", "http://localhost:8000"))
