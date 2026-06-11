"""FasterWhisperTranscriber / FallbackTranscriber / GILJOBE_STT 배선 — 오프라인.

실 모델 로드 없이 검증한다: 모델은 주입 seam(`model=`)으로 더블을 꽂는다
(critic의 client 주입과 동형). 실 whisper 품질/레이턴시는 .dev/audio/stt_whisper_compare.py
(수동 evidence)가 담당 — 테스트는 변환·계약·폴백 분기만 본다.
"""
from __future__ import annotations

import numpy as np
import pytest

from giljobe.analysis.transcriber import FallbackTranscriber, FasterWhisperTranscriber
from giljobe.server.__main__ import _build_transcriber


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    """transcribe 호출을 기록하고 고정 세그먼트를 돌려주는 더블(상태 없는 순수 지향)."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        return [_Seg(t) for t in self.texts], None


class _BoomTranscriber:
    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        raise RuntimeError("vLLM down")


class _OkTranscriber:
    def __init__(self, text: str = "정상 전사") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        self.calls += 1
        return self.text


def test_empty_pcm_returns_empty_without_model_load():
    # 빈 윈도우는 모델 로드 자체가 없어야 한다(폴백 대기 비용 0 계약).
    t = FasterWhisperTranscriber(model=None)
    assert t.transcribe(b"") == ""
    assert t._model is None


def test_pcm_converted_to_normalized_float32_and_korean_forced():
    fake = _FakeWhisperModel(["안녕하세요", "반갑습니다"])
    t = FasterWhisperTranscriber(model=fake)
    pcm = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
    out = t.transcribe(pcm)
    assert out == "안녕하세요 반갑습니다"
    call = fake.calls[0]
    audio = call["audio"]
    assert audio.dtype == np.float32
    assert audio.tolist() == pytest.approx([0.0, 0.5, -1.0])
    assert call["language"] == "ko"
    assert call["vad_filter"] is False


def test_non_16k_sample_rate_fails_loudly():
    # 조용한 오전사보다 명시 실패 — ndarray 경로는 16k 가정이 깨지면 텍스트가 통째로 깨진다.
    t = FasterWhisperTranscriber(model=_FakeWhisperModel(["x"]))
    with pytest.raises(ValueError, match="16kHz"):
        t.transcribe(b"\x00\x00", sample_rate=48000)


def test_fallback_uses_primary_when_healthy():
    primary, fallback = _OkTranscriber("primary"), _OkTranscriber("fallback")
    out = FallbackTranscriber(primary, fallback).transcribe(b"\x00\x00")
    assert out == "primary"
    assert fallback.calls == 0


def test_fallback_switches_on_primary_failure():
    fallback = _OkTranscriber("whisper 결과")
    out = FallbackTranscriber(_BoomTranscriber(), fallback).transcribe(b"\x00\x00")
    assert out == "whisper 결과"
    assert fallback.calls == 1


def test_fallback_propagates_when_both_fail():
    # 둘 다 죽으면 전파 — _do_stt가 이 윈도우만 누락 처리(침묵 빈 전사로 강등하지 않기).
    with pytest.raises(RuntimeError):
        FallbackTranscriber(_BoomTranscriber(), _BoomTranscriber()).transcribe(b"\x00\x00")


class _NullClient:
    pass


def test_build_transcriber_env_wiring(monkeypatch):
    monkeypatch.delenv("GILJOBE_STT", raising=False)
    assert _build_transcriber(_NullClient()) is None  # 기본 = gemma(현행 무변경)

    monkeypatch.setenv("GILJOBE_STT", "whisper")
    monkeypatch.setenv("GILJOBE_WHISPER_MODEL", "large-v3-turbo")
    t = _build_transcriber(_NullClient())
    assert isinstance(t, FasterWhisperTranscriber)
    assert t.model_name == "large-v3-turbo"

    monkeypatch.setenv("GILJOBE_STT", "auto")
    t = _build_transcriber(_NullClient())
    assert isinstance(t, FallbackTranscriber)
    assert isinstance(t.fallback, FasterWhisperTranscriber)

    monkeypatch.setenv("GILJOBE_STT", "wisper")  # 오타는 기동 실패로 드러나야 한다
    with pytest.raises(ValueError, match="GILJOBE_STT"):
        _build_transcriber(_NullClient())
