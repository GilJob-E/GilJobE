"""LiveKit 룸 hidden 구독자 — 통합 타깃 입력 어댑터.

브라우저가 LiveKit 룸 `giljob-session-{id}`에 publish한 candidate track을 우리가 hidden participant로
join해 구독하고, ingest glue(consume_*_lk)로 윈도워에 흘려 critic/STT → 레코드를 송출한다. PLAN의
aiortc `POST /offer` 직수신을 대체(NEXT.md Slice 8 피벗).

레이어링: 룸 lifecycle은 여기(server). media는 consume_*_lk glue까지(leaf — livekit 미import).
windowing/emit/recorder/sink는 무변경 재사용. 실 rtc.VideoStream/AudioStream 생성만 server가 하고
(주입 가능한 stream factory), ingest 소비자는 그 async-iterable을 덕타이핑으로 소비한다.

★ 동시성(PLAN §리스크 "이벤트루프 블로킹"):
  - 윈도워 워커(critic/STT, 3–7s)는 executor에서 돈다(ingest/콜백에서 직접 호출 금지).
  - 워커→루프 복귀는 **call_soon_threadsafe** 한 길로만(make_signal_bridge → queue → drain). 그래서
    recorder/sink가 전부 루프 스레드에서 돌아 비스레드세이프 sink(SSE StreamResponse)도 안전.
  - room.on 콜백은 동기 → 비동기 작업은 create_task로 띄운다(콜백 안에서 await 금지).

★ poll-clock 계약(slice-07 핸드오프): ingest는 프레임을 미디어 상대초(첫 프레임=0)로 태깅하고,
  서버가 poll(now)를 같은 타임라인으로 몬다. now=loop.time()-t0, t0=첫 track 구독 시각(프레임 도착
  직전이라 근사 일치). 타이머 기반이라 프레임이 끊겨도(침묵) 완성된 윈도우를 계속 flush한다.
"""
from __future__ import annotations

import asyncio
import logging

from giljobe.media.ingest import (
    SAMPLE_RATE,
    consume_audio_lk,
    consume_video_lk,
)

logger = logging.getLogger(__name__)

# LiveKit enum 값(실측, `.dev/livekit-sdk-research.md`): server가 import 시점에 livekit을 강제로
# 끌어오지 않도록 상수로 박는다(유닛테스트는 livekit 없이 fake로 가능 — 실 stream factory만 lazy import).
VIDEO_RGB24 = 4  # rtc.VideoBufferType.RGB24
KIND_AUDIO = 1   # rtc.TrackKind.KIND_AUDIO
KIND_VIDEO = 2   # rtc.TrackKind.KIND_VIDEO


def make_signal_bridge(loop, queue):
    """윈도워 on_signal 콜백을 만든다 — 워커 스레드에서 호출돼 신호를 이벤트루프 큐로 마샬링(비차단).
    드레인 태스크가 루프 스레드에서 큐를 비워 recorder/sink로 흘린다(PLAN §5 worker→loop 브리지)."""

    def on_signal(sig) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, sig)

    return on_signal


def _default_video_stream(track, fmt):
    from livekit import rtc

    return rtc.VideoStream(track, format=fmt)


def _default_audio_stream(track, sample_rate, num_channels):
    from livekit import rtc

    return rtc.AudioStream(track, sample_rate=sample_rate, num_channels=num_channels)


class LiveKitSubscriber:
    """LiveKit 룸 구독자 세션 — 한 인터뷰(단일 세션·단일 턴) 입력 어댑터.

    수명: start()(브리지/핸들러/드레인·poll 태스크) → connect(url, token)(실 접속+기존 track 구독)
    → (candidate publish 동안 소비) → aclose(now)(턴 종료 flush + 정리). 또는 run(url, token)으로
    disconnected까지 한 번에.
    """

    def __init__(
        self,
        room,
        windower,
        recorder,
        *,
        poll_interval: float = 0.5,
        video_format: int = VIDEO_RGB24,
        audio_sample_rate: int = SAMPLE_RATE,
        audio_channels: int = 1,
        video_stream_factory=_default_video_stream,
        audio_stream_factory=_default_audio_stream,
    ) -> None:
        self.room = room
        self.windower = windower
        self.recorder = recorder
        self.poll_interval = poll_interval
        self.video_format = video_format
        self.audio_sr = audio_sample_rate
        self.audio_ch = audio_channels
        self._vstream = video_stream_factory
        self._astream = audio_stream_factory

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stopping: asyncio.Event | None = None
        self._t0: float | None = None         # poll-clock 원점(첫 track 구독 시각, loop.time 기준)
        self._consume_tasks: set[asyncio.Task] = set()
        self._poll_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._consumed_sids: set[str] = set()  # 중복 구독 방지(track_subscribed ↔ 기존 track 순회)
        self._started = False

    # ── 수명 ──
    async def start(self) -> None:
        """브리지/핸들러를 걸고 드레인·poll 태스크를 띄운다(connect 전). 턴을 reset로 시작."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stopping = asyncio.Event()
        self.windower.on_signal = make_signal_bridge(self._loop, self._queue)
        self._register_handlers()
        self.windower.reset()    # 접속=턴 시작(PLAN §5 데모 단순화; 통합 턴 경계는 외부 트리거가 추후)
        self.recorder.reset()
        self._drain_task = self._loop.create_task(self._drain_loop())
        self._poll_task = self._loop.create_task(self._poll_loop())
        self._started = True

    async def connect(self, url: str, token: str) -> None:
        """실 LiveKit 접속(auto_subscribe) 후, 이미 publish된 track을 직접 구독(늦은 join 대비)."""
        from livekit import rtc

        await self.room.connect(url, token, rtc.RoomOptions(auto_subscribe=True))
        self._subscribe_existing()

    async def run(self, url: str, token: str) -> None:
        """편의: start→connect→disconnected까지 대기→aclose. 취소되면 정리 후 전파."""
        await self.start()
        await self.connect(url, token)
        try:
            await self._stopping.wait()
        finally:
            await self.aclose()

    async def aclose(self, now: float | None = None) -> None:
        """턴 종료 flush(end_turn) + 태스크/룸 정리. 멱등(중복 호출 안전).

        ★ windower.close()(소유 executor shutdown) + 소비 태스크 cancel + room disconnect는 end_turn/
        _flush_queue가 예외나 **취소**(클라이언트가 /subscriber/stop 도중 끊김 등)로 끊겨도 반드시 실행한다
        — 안 그러면 매 턴 7개 스레드풀이 영구 누수(적대리뷰 확정). 동기 close()를 finally 첫 줄에 둔다."""
        if not self._started:
            return
        self._started = False
        if now is None:
            now = self._media_now()
        # poll 먼저 멈춘다(더는 새 윈도우 제출 안 함).
        if self._poll_task is not None:
            self._poll_task.cancel()
        try:
            # 턴 종료는 executor에서 — end_turn은 nv/stt/tail future를 기다리며(블로킹) trailing window
            # 신호 + turn_end를 emit한다. 그동안 드레인 루프가 루프 스레드에서 그 신호들을 recorder로 흘림.
            if self._loop is not None:
                try:
                    await self._loop.run_in_executor(None, self.windower.end_turn, now)
                except Exception:  # noqa: BLE001 - 종료 경로: end_turn 실패해도 정리는 계속(가시성 위해 로깅)
                    logger.exception("aclose: end_turn 실패")
            # 드레인 루프를 멈추고, 남은 신호(turn_end 등)를 동기로 마저 비운다.
            if self._drain_task is not None:
                self._drain_task.cancel()
            await self._flush_queue()
        finally:
            # ★ 동기 close()를 finally 첫 줄에 — 위가 예외·취소로 끊겨도 소유 executor를 반드시 shutdown.
            self.windower.close()  # _closed 가드 → 뒤늦은 풀평가 emit no-op(안전), 멱등
            # 스냅샷 후 비우고 취소 — 종료 중 _on_track은 _started 가드로 막혀 새 태스크가 안 끼어든다.
            consume_tasks = list(self._consume_tasks)
            self._consume_tasks.clear()
            for t in consume_tasks:
                t.cancel()
            await self._disconnect_room()

    # ── 트랙 구독·디스패치 ──
    def _register_handlers(self) -> None:
        @self.room.on("track_subscribed")
        def _on_subscribed(track, publication, participant):  # noqa: ANN001 - livekit 콜백 시그니처
            self._on_track(track)

        @self.room.on("disconnected")
        def _on_disconnected(*_args):  # noqa: ANN002
            if self._stopping is not None:
                self._stopping.set()

    def _subscribe_existing(self) -> None:
        """이미 publish된 track 구독 — 늦게 join하면 track_subscribed가 안 올 수 있다(연구 노트 §track)."""
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = getattr(pub, "track", None)
                if track is not None:
                    self._on_track(track)
                else:  # 아직 구독 전 → 구독 요청(auto_subscribe면 곧 track_subscribed가 옴)
                    set_sub = getattr(pub, "set_subscribed", None)
                    if set_sub is not None:
                        set_sub(True)

    def _on_track(self, track) -> None:
        """video/audio track을 알맞은 consume_*_lk 태스크로 띄운다(콜백=동기 → create_task)."""
        if not self._started:  # 종료(aclose) 후 늦게 온 track 이벤트 무시 — orphan 소비 태스크 방지
            return
        sid = getattr(track, "sid", None)
        if sid is not None and sid in self._consumed_sids:
            return  # 중복(track_subscribed ↔ 기존 track 순회) 방지
        if sid is not None:
            self._consumed_sids.add(sid)
        if self._t0 is None:  # poll-clock 원점 = 첫 track(프레임 도착 직전)
            self._t0 = self._loop.time()
        kind = getattr(track, "kind", None)
        if kind == KIND_VIDEO:
            # 비전 그라운딩 레인이 켜져 있으면 dense 피드(풀 fps, throttle 전)를 함께 연결.
            dense = (
                self.windower.add_dense_frame
                if getattr(self.windower, "grounder", None) is not None
                else None
            )
            coro = consume_video_lk(self._vstream(track, self.video_format), self.windower, dense=dense)
        elif kind == KIND_AUDIO:
            stream = self._astream(track, self.audio_sr, self.audio_ch)
            coro = consume_audio_lk(stream, self.windower)
        else:
            return  # data 등 비미디어 track은 무시
        task = self._loop.create_task(coro)
        self._consume_tasks.add(task)
        task.add_done_callback(self._on_consume_done)

    def _on_consume_done(self, task: asyncio.Task) -> None:
        """소비 태스크 정리 + 예외 로깅(취소 제외). 윈도워 워커와 동일하게 'silent drop 방지' —
        encode_jpeg_rgb/변환 등이 던진 예외를 조용히 흘리지 않고 남긴다(track 소비는 그래도 중단됨)."""
        self._consume_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("consume 태스크 실패(해당 track 소비 중단)", exc_info=exc)

    # ── 루프(poll·drain) ──
    def _media_now(self) -> float:
        """미디어 상대초(poll-clock). 첫 track 전이면 0(poll no-op)."""
        if self._t0 is None or self._loop is None:
            return 0.0
        return self._loop.time() - self._t0

    def media_now(self) -> float:
        """공개 미디어 시계 — 외부 이벤트(realtime sideband)를 윈도워 타임라인에 정렬할 때 쓴다."""
        return self._media_now()

    async def _poll_loop(self) -> None:
        """주기적으로 windower.poll(미디어시계) — 프레임이 끊겨도 완성 윈도우를 flush(타이머 구동)."""
        while True:
            await asyncio.sleep(self.poll_interval)
            now = self._media_now()
            if now > 0.0:
                self.windower.poll(now)

    async def _drain_loop(self) -> None:
        """큐(워커→루프 마샬링)를 비워 recorder로 흘린다 — 전 sink emit이 루프 스레드에서 일어남."""
        assert self._queue is not None
        while True:
            sig = await self._queue.get()
            self.recorder.on_signal(sig)

    async def _flush_queue(self, idle_ticks: int = 3) -> None:
        """종료 시 큐에 남은 신호(turn_end 등)를 동기로 비운다. call_soon_threadsafe로 들어온
        콜백이 루프에 반영되도록 잠깐씩 양보하며, 연속 idle이 idle_ticks 되면 종료."""
        assert self._queue is not None
        idle = 0
        while idle < idle_ticks:
            try:
                sig = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                idle += 1
                await asyncio.sleep(0.005)  # 스케줄된 put_nowait 콜백 실행 기회
                continue
            idle = 0
            self.recorder.on_signal(sig)

    async def _disconnect_room(self) -> None:
        disconnect = getattr(self.room, "disconnect", None)
        if disconnect is None:
            return
        try:
            res = disconnect()
            if asyncio.iscoroutine(res):
                await res
        except Exception:  # noqa: BLE001 - 종료 경로: 끊기 실패는 로깅만
            logger.exception("room disconnect 실패")
