"""테스트 더블 — vLLM/ffmpeg/GPU 없이 critic·transcriber 표면을 흉내낸다.

windowing(Slice 4)·emit(Slice 5)·e2e mock(Slice 6) 배선을 모델 없이 검증하기 위한 것.
`MockCritic`은 `WindowCritic`의 메서드 시그니처를 그대로 미러링하되 고정 신호를 즉시 낸다
(vLLM/ffmpeg 호출 없음). `SlowMockCritic`은 거기에 레인별 sleep을 얹어 느린 eval 레인이
nv/stt 레인을 막지 않는지(레인 독립성)를 테스트할 수 있게 한다.

설계 주의: 이 더블들은 windowing의 스레드풀 레인에서 **동시에** 호출된다. 그래서
호출 카운터 같은 공유 상태를 두지 않고 **입력의 순수 함수**로 유지한다(동시 증가로 인한
플레이크 회피). 호출 횟수·cadence 검증은 windower가 (loop에서 직렬화해) 모은 레코드로 한다.
"""
from __future__ import annotations

import time

from giljobe.models.signals import EvaluationSignal, NonVerbalSignal

from .transcriber import Transcriber


class MockTranscriber:
    """고정 문자열을 내는 `Transcriber` 구현. 빈 PCM이면 빈 문자열(실 transcriber 계약과 동일)."""

    def __init__(self, text: str = "목 전사 텍스트입니다") -> None:
        self.text = text

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        return self.text if pcm else ""


class MockCritic:
    """`WindowCritic`의 메서드 표면을 미러링하는 오프라인 더블. 고정 신호를 즉시 반환하고
    vLLM/ffmpeg를 일절 부르지 않는다. 윈도우 좌표(t/window_s)는 신호에 그대로 echo해
    테스트가 윈도우 배선(어느 윈도우의 신호인지)을 assert할 수 있게 한다.

    전사는 실 critic과 똑같이 주입된 `Transcriber`(기본 `MockTranscriber`)에 위임한다.
    """

    def __init__(
        self,
        *,
        transcriber: Transcriber | None = None,
        state: str = "attentive",
        intensity: float = 0.5,
        note: str = "mock 비언어 신호",
    ) -> None:
        self.transcriber = transcriber or MockTranscriber()
        self.state = state
        self.intensity = intensity
        self.note = note

    def warmup(self) -> None:
        """실 critic 표면 호환용 no-op (모델이 없어 데울 것이 없다)."""

    def read_nonverbal(
        self,
        jpeg_frames: list[bytes],
        pcm: bytes,
        *,
        t: float,
        window_s: float,
        src_fps: float = 1.0,
        sample_rate: int = 16000,
        vision: dict | None = None,
        prosody: dict | None = None,
    ) -> NonVerbalSignal:
        self.last_nv_kwargs = {"vision": vision, "prosody": prosody}  # 주입 배선 테스트용 캡처
        return NonVerbalSignal(
            t=t, window_s=window_s, state=self.state, intensity=self.intensity, note=self.note,
        )

    def transcribe_window(
        self,
        pcm: bytes,
        *,
        t: float,
        window_s: float,
        sample_rate: int = 16000,
    ) -> str:
        return self.transcriber.transcribe(pcm, sample_rate=sample_rate)

    def evaluate_window(
        self,
        jpeg_frames: list[bytes],
        pcm: bytes,
        *,
        window_start_s: float,
        window_dur_s: float,
        src_fps: float = 1.0,
        sample_rate: int = 16000,
        compact: bool = False,
        prosody: dict | None = None,
        vision: dict | None = None,
    ) -> EvaluationSignal:
        if compact:  # compact tail은 실 critic처럼 2키 최소 스키마 모양으로
            return EvaluationSignal(
                window_start_s=window_start_s,
                window_dur_s=window_dur_s,
                critique=["mock compact 약점"],
                key_observations=["mock compact 요약"],
                compact=True,
            )
        return EvaluationSignal(
            window_start_s=window_start_s,
            window_dur_s=window_dur_s,
            verbal={"logic": "mock", "structure": "mock", "specificity": "mock"},
            vocal={"volume": "mock", "pace": "mock", "pauses": "mock", "intonation": "mock"},
            visual={"eye_contact": "mock", "posture": "mock", "expression": "mock", "gesture_over_time": "mock"},
            critique=["mock 약점"],
            key_observations=["mock 관찰"],
        )


class SlowMockCritic(MockCritic):
    """레인별로 sleep을 얹은 `MockCritic`. 느린 eval 레인이 nv/stt 레인을 막지 않는지
    (레인 독립성) 테스트하기 위한 것. 기본은 eval만 느리게(현실에서 16s eval이 무거운 레인).
    각 레인 지연은 생성자에서 따로 조절한다.
    """

    def __init__(
        self,
        *,
        nv_delay: float = 0.0,
        stt_delay: float = 0.0,
        eval_delay: float = 0.5,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.nv_delay = nv_delay
        self.stt_delay = stt_delay
        self.eval_delay = eval_delay

    def read_nonverbal(self, *args, **kwargs) -> NonVerbalSignal:
        if self.nv_delay:
            time.sleep(self.nv_delay)
        return super().read_nonverbal(*args, **kwargs)

    def transcribe_window(self, *args, **kwargs) -> str:
        if self.stt_delay:
            time.sleep(self.stt_delay)
        return super().transcribe_window(*args, **kwargs)

    def evaluate_window(self, *args, **kwargs) -> EvaluationSignal:
        if self.eval_delay:
            time.sleep(self.eval_delay)
        return super().evaluate_window(*args, **kwargs)
