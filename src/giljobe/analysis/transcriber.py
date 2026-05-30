"""윈도우 PCM → 전사 텍스트.

critic은 STT를 직접 박지 않고 `Transcriber` 뒤로 위임한다(주입). 기본 구현은
`GemmaNativeTranscriber` — 비언어 비평과 **같은** vLLM E4B를 그대로 써서 전사하므로
VRAM 추가가 0이고 별도 STT 엔진을 안 띄운다. 이 기본값이 충분한지(요약·번역으로 새지
않는지)는 Slice 2 스파이크(`tests/test_spike_transcribe.py`)가 게이팅한다 — FAIL이면
`FasterWhisperTranscriber`(폴백, S3에서 추가)로 교체.
"""
from __future__ import annotations

from typing import Protocol

from giljobe.llm.vllm_client import VllmClient, default_vllm_client
from giljobe.media.window_assembly import assemble_audio_url

MODEL = "google/gemma-4-E4B-it"

# 전사기로만 못박는 프롬프트. E4B는 같은 모델로 비평/요약/번역도 하므로, 명시적으로
# "받아쓰기만, 요약·번역·평가 금지, 없으면 빈 문자열, JSON·설명 없이 텍스트만"을 강제한다.
TRANSCRIBE_SYSTEM = (
    "당신은 한국어 음성 전사기입니다. 오디오에서 들리는 말을 *들리는 그대로* 한국어로 "
    "정확히 받아쓰세요. 요약·번역·해석·평가·교정을 하지 말고, 발화된 텍스트만 출력하세요. "
    "음성이 없거나 알아들을 수 없으면 빈 문자열을 출력하세요. "
    "JSON·따옴표·머리말·설명 없이 오직 전사 텍스트만 출력하세요."
)


class Transcriber(Protocol):
    """윈도우 PCM(16k mono Int16) → 전사 텍스트. 빈 음성이면 빈 문자열."""

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str: ...


class GemmaNativeTranscriber:
    """같은 vLLM E4B(네이티브 AV)로 전사 — VRAM 추가 0. 비평과 달리 JSON 파싱 안 하고
    plain text를 그대로 반환한다(전사는 구조화 신호가 아니라 텍스트라서)."""

    def __init__(
        self,
        *,
        client: VllmClient | None = None,
        model: str = MODEL,
        max_tokens: int = 256,
    ) -> None:
        self.client = client or default_vllm_client()
        self.model = model
        self.max_tokens = max_tokens

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        content = [
            {
                "type": "audio_url",
                "audio_url": {"url": assemble_audio_url(pcm, sample_rate=sample_rate)},
            },
            # user 지시문을 영어로 둔다 — 한국어로 쓰면 음성과 같은 언어라 모델이 발화 내용으로
            # 착각해 전사 끝에 echo한다(실측, Slice 2 스파이크). 영어 directive는 누수 없음.
            {"type": "text", "text": "Transcribe."},
        ]
        resp = self.client.chat(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": TRANSCRIBE_SYSTEM},
                    {"role": "user", "content": content},
                ],
                "max_tokens": self.max_tokens,
                "temperature": 0.0,
            }
        )
        return resp["choices"][0]["message"]["content"].strip()
