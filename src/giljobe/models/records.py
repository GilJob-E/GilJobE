"""emit 레이어가 송출하는 JSON 레코드 스키마 (면접관 LLM이 먹는 .json).

models/signals.py의 in-process 신호(NonVerbal/Transcript/Eval/TurnEnd)는 *윈도워가 내는
도메인 이벤트*다. 여기 레코드는 그걸 **소비자용 JSON**으로 직렬화한 형태 — emit/sink.py의
SignalRecorder가 nv·전사를 윈도우별로 병합(채널① window)하고, 발화중 풀 평가(채널② eval)와
턴 종료(turn_end)를 각각 레코드로 만든다. 모든 레코드는 `type`과 `t`로 self-describing —
신호가 완료순(시간순 아님)으로 나오므로 소비자가 `t`로 정렬한다(PLAN §4).

스키마 3종: window(3s nv+전사) / eval(16s 풀 평가) / turn_end(전사 합본+compact eval).
"""
from __future__ import annotations

from dataclasses import dataclass

from giljobe.models.signals import (
    EvaluationSignal,
    NonVerbalSignal,
    SentenceSignal,
    TranscriptSignal,
    TurnEnd,
)


@dataclass
class WindowRecord:
    """채널① 윈도우 레코드 — 한 3s 윈도우의 비언어 read + 같은 윈도우 전사를 **병합**.
    nv 레인과 stt 레인이 같은 (t, window_s)로 신호를 내므로 SignalRecorder가 t로 페어링한다.
    한 레인만 올 수 있다(프레임 없으면 nonverbal=None, 오디오 없으면 transcript="")."""

    session_id: str
    t: float                # 윈도우 중심 시각(초) — 페어링·정렬 키
    window_s: float
    nonverbal: dict | None  # {state, intensity, note} 또는 None(비디오 없던 윈도우)
    transcript: str         # 이 윈도우 전사("" = 무음/오디오 없음)
    transcript_final: bool  # 이 윈도우 전사 확정 여부(TranscriptSignal 수신=True)
    # 객관 비전 측정치(MediaPipe 윈도우 집계) — 모델 read와 별개의 ground truth(inject AND emit).
    objective_nonverbal: dict | None = None
    type: str = "window"

    @classmethod
    def from_pair(
        cls,
        session_id: str,
        *,
        t: float,
        window_s: float,
        nv: NonVerbalSignal | None,
        tx: TranscriptSignal | None,
    ) -> WindowRecord:
        nonverbal = (
            {"state": nv.state, "intensity": nv.intensity, "note": nv.note}
            if nv is not None
            else None
        )
        return cls(
            session_id=session_id,
            t=t,
            window_s=window_s,
            nonverbal=nonverbal,
            transcript=tx.text if tx is not None else "",
            transcript_final=tx is not None,
            objective_nonverbal=nv.objective if nv is not None else None,
        )

    def to_dict(self) -> dict:
        # type을 앞에 두어 소비자가 먼저 디스패치할 수 있게(필드 순서는 가독성용).
        return {
            "type": self.type,
            "session_id": self.session_id,
            "t": self.t,
            "window_s": self.window_s,
            "nonverbal": self.nonverbal,
            "transcript": self.transcript,
            "transcript_final": self.transcript_final,
            "objective_nonverbal": self.objective_nonverbal,
        }


@dataclass
class EvalRecord:
    """채널② 풀 평가 레코드 — 발화중 16s 윈도우 평가(EvaluationSignal, compact=False).
    소비자가 집계할 재료(집계는 다운스트림 몫). t는 윈도우 중심(window/turn_end와 정렬 일관성)."""

    session_id: str
    t: float
    window_s: float
    eval: dict  # EvaluationSignal.to_dict() 전체(verbal/vocal/visual/critique/...)
    type: str = "eval"

    @classmethod
    def from_signal(cls, session_id: str, sig: EvaluationSignal) -> EvalRecord:
        return cls(
            session_id=session_id,
            t=sig.window_start_s + sig.window_dur_s / 2,  # nv와 같은 '중심' 기준으로 t-정렬 일관
            window_s=sig.window_dur_s,
            eval=sig.to_dict(),
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "t": self.t,
            "window_s": self.window_s,
            "eval": self.eval,
        }


@dataclass
class SentenceRecord:
    """문장 레코드(외부 전사 모드) — 한 문장 구간의 전사+실측+nv read 묶음. 페어링 없이
    완결 단위로 도착 즉시 송출. t=구간 중심(window/eval과 같은 정렬 기준)."""

    session_id: str
    t: float
    start_s: float
    end_s: float
    text: str
    source: str
    speech: dict | None
    visual: dict | None
    nonverbal: dict | None
    type: str = "sentence"

    @classmethod
    def from_signal(cls, session_id: str, sig: SentenceSignal) -> SentenceRecord:
        return cls(
            session_id=session_id,
            t=round((sig.start_s + sig.end_s) / 2, 2),
            start_s=sig.start_s,
            end_s=sig.end_s,
            text=sig.text,
            source=sig.source,
            speech=sig.speech,
            visual=sig.visual,
            nonverbal=sig.nonverbal,
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "t": self.t,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "text": self.text,
            "source": self.source,
            "speech": self.speech,
            "visual": self.visual,
            "nonverbal": self.nonverbal,
        }


@dataclass
class TurnEndRecord:
    """턴 종료 레코드 — transcript_full(면접관 LLM의 실제 입력) + 선택 compact eval."""

    session_id: str
    t: float
    transcript_full: str
    eval: dict | None  # compact EvaluationSignal.to_dict() 또는 None
    type: str = "turn_end"

    @classmethod
    def from_turn_end(cls, session_id: str, te: TurnEnd) -> TurnEndRecord:
        return cls(
            session_id=session_id,
            t=te.t,
            transcript_full=te.transcript_full,
            eval=te.eval.to_dict() if te.eval is not None else None,
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "t": self.t,
            "transcript_full": self.transcript_full,
            "eval": self.eval,
        }
