"""gje 출력 신호 스키마 (잠정 — M5 evidence로 보정 예정).

gje는 분석 신호만 낸다. **집계는 Gemini 몫**이라 여기엔 종합/병합 타입이 없다.
두 채널:
  ① NonVerbalSignal  — 발화 중 지원자 비언어 read (저지연 연속, ~1–3s cadence).
  ② EvaluationSignal — 16초 윈도우 단위 verbal/vocal/visual 평가.

소비자(GilJob)가 ①을 아바타 제스처로 매핑하고 ②를 Gemini에 주입한다 — 그 매핑/주입은
이 모듈 밖. 필드는 docs/PLAN.md "출력 계약" 기준 잠정값이며, 실제 E4B가 신뢰성 있게
읽어내는 것만 M5에서 확정한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class NonVerbalSignal:
    """채널 ① — 한 짧은 윈도우에서 읽은 지원자 비언어 상태 (아바타 반응의 근거)."""

    t: float  # 턴 시작 기준 윈도우 중심 시각(초)
    window_s: float  # 이 read가 커버한 구간 길이(초)
    state: str  # 잠정 라벨 집합: engaged|attentive|hesitant|nervous|confident|disengaged|neutral
    intensity: float  # 0.0–1.0
    note: str = ""  # 모델이 짚은 짧은 근거(선택)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvaluationSignal:
    """채널 ② — 한 16초 윈도우의 verbal/vocal/visual 평가 (Gemini가 집계할 재료)."""

    window_start_s: float
    window_dur_s: float
    verbal: dict = field(default_factory=dict)  # {logic, structure, specificity, ...}
    vocal: dict = field(default_factory=dict)  # {volume, pace, pauses, intonation, ...}
    visual: dict = field(default_factory=dict)  # {eye_contact, posture, expression, gesture_over_time, ...}
    critique: list[str] = field(default_factory=list)  # 면접관으로서 걸리는 약점·우려(강제)
    key_observations: list[str] = field(default_factory=list)
    compact: bool = False  # end-of-turn tail(짧은 출력)이면 True — 발화중 풀 비평과 구분

    def to_dict(self) -> dict:
        return asdict(self)


# 잠정 라벨 집합 — M5에서 E4B가 실제로 구분 가능한지 검증 후 확정/축소.
NONVERBAL_STATES = (
    "engaged",
    "attentive",
    "hesitant",
    "nervous",
    "confident",
    "disengaged",
    "neutral",
)
