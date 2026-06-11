"""송출 — 윈도워의 완료순 신호 스트림을 소비자용 JSON 레코드로 직렬화해 Sink로 보낸다.

SignalRecorder가 (1) nv·전사를 윈도우별로 t-페어링해 병합(WindowRecord), (2) 발화중 풀
평가→EvalRecord, (3) TurnEnd→TurnEndRecord 로 만들고 등록된 Sink들에 fan-out한다. Sink는
전송만 담당(JSONL append / SSE 프레임 broadcast) — 레코드 스키마는 models/records.py.

조인: nv·stt 레인은 같은 (t, window_s)로 신호를 낸다. 둘 다 도착하면 그 즉시 병합 레코드를
emit(실시간 UX), 한 레인만 온 윈도우(프레임/오디오 결손)는 turn_end에서 sweep해 부분 레코드로
flush. 완료순 emit이라 레코드는 t로 self-describing — 소비자가 t로 정렬(PLAN §4 리스크).

스레딩: on_signal은 윈도워 워커 스레드들에서 동시에 불릴 수 있다(서버가 call_soon_threadsafe로
직렬화하면 단일 스레드지만, on_signal=recorder.on_signal로 직접 꽂으면 멀티). 그래서 조인 상태는
lock으로 보호하고, sink fan-out은 lock 밖에서 한다(느린 sink가 조인을 막지 않게 — 윈도워의
'build under lock, act outside lock' 관례). 한 Sink 실패는 로깅하고 격리(silent drop 방지).

import: emit은 **models만** 의존(pipeline 의존 금지 — 그래서 윈도워가 계약을 models/signals.py에
뒀다). WebhookSink는 실제 다운스트림 URL이 생기면 그때 추가(PLAN §4; 지금은 SSE + JSONL).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from giljobe.models.records import EvalRecord, SentenceRecord, TurnEndRecord, WindowRecord
from giljobe.models.signals import (
    EvaluationSignal,
    NonVerbalSignal,
    SentenceSignal,
    TranscriptSignal,
    TurnEnd,
)

logger = logging.getLogger(__name__)


class Sink(Protocol):
    """레코드 전송 표면. emit은 비차단이어야 한다(SignalRecorder가 워커 스레드에서 부를 수 있음)."""

    def emit(self, record: dict) -> None: ...


def sse_frame(record: dict) -> str:
    r"""레코드를 SSE 이벤트 프레임으로 직렬화: ``data: {json}\n\n``.
    json.dumps가 개행을 이스케이프하므로 한 레코드=한 data 라인(프레임 깨짐 없음).
    한국어 전사 보존 위해 ensure_ascii=False(SSE/JSONL 모두 UTF-8)."""
    return f"data: {json.dumps(record, ensure_ascii=False)}\n\n"


class JSONLSink:
    """리플레이/감사 로그 — 레코드 1개당 JSON 1줄을 파일에 append. 라이브 소비자 없이도
    테스트가 출력을 assert할 수 있게 한다(PLAN §4). 라인 단위 atomic write(스레드 안전)."""

    def __init__(self, path) -> None:
        self._f = open(path, "a", encoding="utf-8")  # noqa: SIM115, PTH123 - close()로 수명 관리
        self._lock = threading.Lock()

    def emit(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:  # write+flush를 한 묶음으로 — 멀티스레드 라인 인터리브 방지
            self._f.write(line)
            self._f.flush()  # 라이브 tail/테스트가 즉시 읽도록

    def close(self) -> None:
        with self._lock:
            self._f.close()


class SSESink:
    """SSE broadcast — ``GET /signals`` 구독자들에게 레코드를 프레임으로 fan-out(1:N).
    구독자는 프레임 문자열을 받는 콜백(server 슬라이스가 aiohttp StreamResponse.write로 연결).
    이 슬라이스는 프레임 포맷+fan-out만 담당(aiohttp glue는 server). 콜백은 비차단 가정 —
    끊긴 구독자(닫힌 연결 등)는 자동 제거하고 나머지 송출은 계속한다."""

    def __init__(self) -> None:
        self._subscribers: set[Callable[[str], None]] = set()
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.discard(callback)

    def emit(self, record: dict) -> None:
        frame = sse_frame(record)
        with self._lock:
            subscribers = list(self._subscribers)  # 스냅샷 — 콜백은 lock 밖에서 호출
        dead: list[Callable[[str], None]] = []
        for cb in subscribers:
            try:
                cb(frame)
            except Exception:  # noqa: BLE001 - 끊긴 구독자는 제거하고 나머지 송출 계속
                dead.append(cb)
        if dead:
            with self._lock:
                self._subscribers.difference_update(dead)


class SignalRecorder:
    """윈도워 on_signal 콜백 — 완료순 신호 스트림을 JSON 레코드로 만들어 Sink들에 fan-out.

    nv·전사 페어링 상태(_pending: t→{nv,tx})만 보유한다. 풀 평가/turn_end는 버퍼 없이
    도착 즉시 변환·송출. turn_end에서 한 레인만 온 잔여 윈도우를 t-정렬해 부분 레코드로 flush한다.
    """

    def __init__(self, session_id: str, sinks: list[Sink]) -> None:
        self.session_id = session_id
        self.sinks = sinks
        self._lock = threading.Lock()
        self._pending: dict[float, dict] = {}  # t → {"nv":NonVerbalSignal|None, "tx":TranscriptSignal|None, "window_s":float}

    def on_signal(self, sig) -> None:
        """신호 타입별로 0개 이상의 레코드를 만들어(조인 상태 변경은 lock 안) fan-out한다."""
        for record in self._build(sig):
            self._fanout(record)

    def reset(self) -> None:
        """/turn/start — 다음 턴을 위해 페어링 버퍼 초기화(윈도워.reset()과 대칭).

        ★ 선결조건(호출자=서버 책임): 이전 턴 레인이 idle일 때 호출(윈도워.reset()과 동일 계약).
        reset 직후 늦게 끝난 이전 턴 nv/stt 신호가 새 _pending 슬롯을 만들면 다음 turn_end에서 stale
        부분 레코드로 샐 수 있으나, 소비자는 t로 자기기술하므로 v1 단일세션에선 범위 밖(YAGNI). 엄밀
        격리가 필요하면 서버가 턴마다 새 TurnWindower+SignalRecorder를 만들거나 신호에 turn epoch를
        실어 드롭한다(server 슬라이스). 잔여 partial은 폐기 전 경고한다(silent drop 방지 — 가시성)."""
        with self._lock:
            dropped = len(self._pending)
            self._pending = {}
        if dropped:  # 정상(턴이 turn_end로 끝남)이면 0 — 0 아니면 인터턴 idle 위반 신호
            logger.warning("SignalRecorder.reset: 짝 못 채운 partial %d개 폐기(이전 턴 잔여)", dropped)

    # ── 신호 → 레코드(dict) ──
    def _build(self, sig) -> list[dict]:
        if isinstance(sig, NonVerbalSignal):
            return self._on_window_signal(sig.t, sig.window_s, nv=sig)
        if isinstance(sig, TranscriptSignal):
            return self._on_window_signal(sig.t, sig.window_s, tx=sig)
        if isinstance(sig, EvaluationSignal):  # 발화중 풀 평가(채널②) → eval 레코드(즉시)
            return [EvalRecord.from_signal(self.session_id, sig).to_dict()]
        if isinstance(sig, SentenceSignal):  # 문장 레인(외부 전사 모드) — 완결 단위, 페어링 불요
            return [SentenceRecord.from_signal(self.session_id, sig).to_dict()]
        if isinstance(sig, TurnEnd):
            return self._on_turn_end(sig)
        return []  # 모르는 신호는 무시(미래 타입에 안전)

    def _on_window_signal(self, t: float, window_s: float, *, nv=None, tx=None) -> list[dict]:
        """nv 또는 전사 1개 수신. 두 레인이 다 모이면 병합 레코드 1개를 즉시 내고 pending에서 제거,
        아니면 짝을 기다린다(한 레인만 끝내 안 오면 turn_end sweep이 부분 레코드로 flush)."""
        with self._lock:
            slot = self._pending.setdefault(t, {"nv": None, "tx": None, "window_s": window_s})
            if nv is not None:
                slot["nv"] = nv
            if tx is not None:
                slot["tx"] = tx
            if slot["nv"] is not None and slot["tx"] is not None:
                del self._pending[t]
                return [self._window_record(t, slot)]
        return []

    def _on_turn_end(self, te: TurnEnd) -> list[dict]:
        """잔여 partial(한 레인만 온 윈도우)을 t-정렬해 flush한 뒤 turn_end 레코드를 낸다."""
        with self._lock:
            leftovers = sorted(self._pending.items())  # (t, slot) t-오름차순
            self._pending = {}
        records = [self._window_record(t, slot) for t, slot in leftovers]
        records.append(TurnEndRecord.from_turn_end(self.session_id, te).to_dict())
        return records

    def _window_record(self, t: float, slot: dict) -> dict:
        return WindowRecord.from_pair(
            self.session_id, t=t, window_s=slot["window_s"], nv=slot["nv"], tx=slot["tx"],
        ).to_dict()

    # ── fan-out (lock 밖, Sink별 격리) ──
    def _fanout(self, record: dict) -> None:
        for sink in self.sinks:
            try:
                sink.emit(record)
            except Exception:  # noqa: BLE001 - 한 Sink 실패가 다른 Sink/윈도워 워커를 죽이지 않게 로깅하고 계속
                logger.exception("sink emit failed: %s", type(sink).__name__)
