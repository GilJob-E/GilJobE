"""분석 서비스 — HTTP 계약(/subscriber/start|stop, /signals) 뒤의 프레임워크 무관 코어.

통합 타깃(`services/analysis-engine`)이 요구하는 계약을 우리가 직접 구현한다. 한 인터뷰의 한 답변
(턴)을 단위로: start=룸 join+턴 시작, stop=턴 flush(end_turn). /signals는 그 턴의 SignalRecorder
레코드를 통합 프론트가 폴링하는 래퍼 모양으로 낸다(transcriptFull=면접관 LLM의 lastAnswer 소스).

★ 설계 핵심(그쪽 자작 wrapper가 낸 버그를 구조적으로 제거): 구독자·드레인·poll·worker→loop
브리지를 전부 **한 영속 이벤트루프**(HTTP 서버 루프) 위에서 돌린다. 그래서
  - 워커 신호가 닫힌 루프로 새지 않고(`Event loop is closed` 폭주 X),
  - 레코드가 **발행 중에도** MemorySink에 누적돼 /signals가 실시간으로 보이며(스톱 후에만 X),
  - 전사(stt 레인)가 정상 동작한다(우리 코드 자체는 Slice 11 대조군서 verbatim 확인).

단일 세션(v1, PLAN §리스크): 동시 1턴. start 시 활성 세션이 있으면 먼저 stop한다. 멀티세션은 범위 밖.
주입(테스트): make_critic/make_config/room_factory를 갈아끼워 GPU·LiveKit 없이 계약을 검증한다.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .app import build_subscriber
from .config import SubscriberConfig

logger = logging.getLogger(__name__)

SERVICE_NAME = "analysis-engine"


class MemorySink:
    """레코드를 메모리에 누적하는 Sink — /signals가 읽어 통합 프론트에 낸다.

    emit은 드레인 루프(서버 루프 스레드)에서 불리지만, /signals 핸들러가 같은 루프에서 동시에
    스냅샷을 읽으므로 lock으로 일관 스냅샷을 보장한다(비차단 — append만)."""

    def __init__(self) -> None:
        self._records: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, record: dict) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> list[dict]:
        with self._lock:
            return list(self._records)


def signals_payload(session_id: str, records: list[dict]) -> dict:
    """SignalRecorder 레코드를 통합 프론트가 폴링하는 래퍼 모양으로 직렬화(계약 — 흑박스 실측 일치).

    transcriptFull = 마지막 turn_end의 transcript_full(=ai-engine lastAnswer 소스).
    rawMediaExposed/rawSecretsExposed=False — 우리는 원시 미디어·시크릿을 절대 노출하지 않는다."""
    window_tx = [
        r.get("transcript", "")
        for r in records
        if r.get("type") == "window" and r.get("transcript", "").strip()
    ]
    turn_ends = [r for r in records if r.get("type") == "turn_end"]
    latest_te = turn_ends[-1] if turn_ends else None
    return {
        "service": SERVICE_NAME,
        "sessionId": session_id,
        "records": records,
        "recordCount": len(records),
        "transcriptFull": (latest_te or {}).get("transcript_full", "") if latest_te else "",
        "windowTranscripts": window_tx,
        "latestTurnEnd": latest_te,
        "rawMediaExposed": False,
        "rawSecretsExposed": False,
    }


@dataclass
class _Session:
    """활성/직전 턴 1개의 상태(구독자 + 그 레코드 sink)."""

    session_id: str
    critic_mode: str
    subscriber: Any
    sink: MemorySink
    started_at: float


class AnalysisService:
    """HTTP 계약 뒤의 턴 lifecycle 관리자. 한 번에 한 세션(턴)만 활성.

    make_critic(mode)->critic, make_config(session_id)->SubscriberConfig, room_factory()->room|None
    을 주입받아 테스트(MockCritic+FakeRoom)와 프로덕션(WindowCritic+실 rtc.Room)이 같은 코어를 쓴다.
    """

    def __init__(
        self,
        *,
        make_critic: Callable[[str], Any],
        make_config: Callable[[str], SubscriberConfig] | None = None,
        room_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
        executors: tuple | None = None,
        make_lanes: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        self._make_critic = make_critic
        self._make_config = make_config or (lambda sid: SubscriberConfig.from_env(session_id=sid))
        self._room_factory = room_factory
        self._clock = clock or _monotonic
        self._executors = executors
        # 객관 그라운딩 레인 팩토리 — start마다 호출해 (grounder, prosody)를 받는다(grounder는
        # 세션 수명이라 재사용 금지 — build_subscriber 계약). None이면 레인 비활성.
        self._make_lanes = make_lanes
        self._active: _Session | None = None
        self._last: _Session | None = None  # stop 후에도 /signals가 그 턴 결과를 읽도록 보존
        # start/stop을 직렬화 — aiohttp가 동시 요청을 별 태스크로 디스패치하므로, _active 확정 전
        # 둘째 start가 단일세션 가드를 통과해 좀비 구독자를 남기는 TOCTOU 경합을 막는다(적대리뷰 확정).
        self._lifecycle_lock = asyncio.Lock()

    async def start(self, session_id: str, critic_mode: str = "window") -> dict:
        """턴 시작 — 룸 join + 윈도워/recorder reset. 활성 세션이 있으면 먼저 stop(단일 세션).
        lifecycle lock으로 동시 start를 직렬화(좀비 구독자 경합 방지)."""
        async with self._lifecycle_lock:
            return await self._start_locked(session_id, critic_mode)

    async def _start_locked(self, session_id: str, critic_mode: str) -> dict:
        if self._active is not None:
            await self._stop_locked()
        config = self._make_config(session_id)
        if config.token is None:
            raise RuntimeError("LiveKit 토큰 미해석(LIVEKIT_TOKEN 또는 LIVEKIT_API_KEY/SECRET 필요)")
        sink = MemorySink()
        critic = self._make_critic(critic_mode)
        room = self._room_factory() if self._room_factory is not None else None
        grounder, prosody = self._make_lanes() if self._make_lanes is not None else (None, None)
        subscriber = build_subscriber(
            critic, [sink], config, room=room, executors=self._executors,
            grounder=grounder, prosody=prosody,
        )
        await subscriber.start()
        try:
            await subscriber.connect(config.url, config.token)
        except Exception:
            # connect 실패(LiveKit 다운/토큰/네트워크) 시 이미 띄운 drain/poll 태스크+윈도워 풀을
            # 멱등 aclose로 정리하고 재전파 — 503 재시도마다 영속 루프에 태스크가 쌓이지 않게(적대리뷰 확정).
            await subscriber.aclose()
            raise
        self._active = _Session(session_id, critic_mode, subscriber, sink, self._clock())
        logger.info("subscriber 시작 session=%s room=%s mode=%s", session_id, config.room, critic_mode)
        return {
            "sessionId": session_id,
            "criticMode": critic_mode,
            "startedAt": self._active.started_at,
            "state": "running",
            "status": "running",
            "threadAlive": True,  # 계약 호환 필드(우리는 스레드 아닌 루프 태스크 — 항상 살아있음)
        }

    async def stop(self) -> dict:
        """턴 종료 — end_turn flush + 정리. 레코드는 _last로 보존해 stop 후 /signals가 답변을 낸다."""
        async with self._lifecycle_lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> dict:
        sess = self._active
        if sess is None:
            return {"status": "idle"}
        self._active = None
        try:
            await sess.subscriber.aclose()
        except Exception:  # noqa: BLE001 - 계약은 항상 stopped; aclose 실패는 로깅만(_active는 이미 비움)
            logger.exception("stop: aclose 실패(계약상 stopped 유지)")
        finally:
            self._last = sess
            logger.info("subscriber 종료 session=%s records=%d", sess.session_id, len(sess.sink.records()))
        return {"status": "stopped"}

    def signals(self, session_id: str | None = None) -> dict:
        """요청 session_id의 레코드를 래퍼 모양으로 — 활성·직전(_last) 둘 다 룩업(새 턴이 시작돼도
        직전 답변이 _last에 남아있으면 회수 가능). 미스매치면 빈 페이로드. 미지정이면 활성 우선."""
        for sess in (self._active, self._last):
            if sess is not None and (session_id is None or session_id == sess.session_id):
                return signals_payload(sess.session_id, sess.sink.records())
        return signals_payload(session_id or "", [])

    @property
    def active_session_id(self) -> str | None:
        return self._active.session_id if self._active is not None else None

    async def aclose(self) -> None:
        """서버 종료 — 활성 세션이 있으면 정리."""
        if self._active is not None:
            await self.stop()


def _monotonic() -> float:
    # 시작시각 라벨용(단조). 테스트는 clock 주입으로 결정적.
    import time

    return time.monotonic()
