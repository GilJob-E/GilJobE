"""윈도우 PCM → 전사 텍스트.

critic은 STT를 직접 박지 않고 `Transcriber` 뒤로 위임한다(주입). 기본 구현은
`GemmaNativeTranscriber` — 비언어 비평과 **같은** vLLM E4B를 그대로 써서 전사하므로
VRAM 추가가 0이고 별도 STT 엔진을 안 띄운다. (Slice 2 스파이크가 이 기본값을 게이팅했다.)

2026-06-11 대개편 #1: `FasterWhisperTranscriber`(Slice 2가 예약한 폴백 자리)를 채웠다.
실 QA 턴(kor.mp4)에서 gemma 전사의 어절 오인식("통학"→"공학" 등)이 확인돼, vLLM 비가용
대비 + 품질 비교를 위해 로컬 whisper 경로가 필요해졌다. 선택은 서버 bootstrap의
`GILJOBE_STT` env가 한다(gemma 기본 — 기존 동작 무변경). 실측 근거:
`.dev/audio/stt_whisper_compare.py` → giljob-docker `qa/stt-comparison.json`.
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

import numpy as np

from giljobe.llm.vllm_client import VllmClient, default_vllm_client
from giljobe.media.window_assembly import assemble_audio_url

logger = logging.getLogger(__name__)

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


class FasterWhisperTranscriber:
    """faster-whisper(CTranslate2) 로컬 STT — vLLM 비가용 폴백 또는 `GILJOBE_STT`로 명시 선택.

    통합 컨테이너는 CPU 전용이라 cpu/int8이 기본이다(이 수치가 곧 프로덕션 레이턴시).
    모델 로드는 첫 transcribe까지 지연 — 폴백으로만 대기할 때 기동/메모리 비용 0.
    """

    def __init__(
        self,
        *,
        model_name: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        model=None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self._model = model  # 테스트 주입 seam(critic의 client 주입과 동형) — None이면 lazy load
        # stt 레인 워커가 2개라 동시 호출이 가능 — lazy load 경합 방지 겸 CPU 추론을 직렬화한다
        # (CPU에선 동시 디코드가 서로를 느리게 할 뿐 이득이 없다. 실측 윈도우당 수백 ms 수준).
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # optional extra — 설치 힌트를 남기고 명시 실패
                raise RuntimeError(
                    "faster-whisper 미설치 — `pip install 'giljobe[whisper]'` 후 사용"
                ) from exc
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        if sample_rate != 16000:
            # ndarray 입력 경로는 16k 고정 가정 — 계약 밖 입력은 조용한 오전사 대신 명시 실패.
            raise ValueError(f"FasterWhisperTranscriber는 16kHz 전용(받음: {sample_rate})")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            model = self._ensure_model()
            # vad_filter=False: 3s 윈도우에선 VAD가 짧은 발화 머리/꼬리를 잘라먹는다(gemma와 동등 조건).
            segments, _ = model.transcribe(audio, language="ko", beam_size=self.beam_size, vad_filter=False)
            return " ".join(s.text.strip() for s in segments).strip()


class FallbackTranscriber:
    """primary 실패 시 fallback 시도 — STT 가용성을 vLLM 단일 장애점에서 분리한다.

    primary 실패는 경고 로깅 후 fallback으로 넘어간다(윈도워 워커 계약과 동형 발상:
    한 윈도우 실패가 레인을 죽이지 않게). fallback까지 실패하면 그대로 전파 —
    호출자(`_do_stt`)가 해당 윈도우만 누락 처리한다.
    """

    def __init__(self, primary: Transcriber, fallback: Transcriber) -> None:
        self.primary = primary
        self.fallback = fallback

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        try:
            return self.primary.transcribe(pcm, sample_rate=sample_rate)
        except Exception:  # noqa: BLE001 - 어떤 실패든 폴백 가치가 있다(타임아웃/HTTP/파싱)
            logger.warning("primary transcriber 실패 — fallback으로 전사 재시도", exc_info=True)
            return self.fallback.transcribe(pcm, sample_rate=sample_rate)
