#!/usr/bin/env python3
"""server.LiveKitSubscriber 단위 테스트 — fake Room/stream으로 배선 검증 (실 LiveKit 네트워크 불요).

라이브 e2e(test_e2e_live_lk.py)는 실 LiveKit 전체 경로를 보지만, 거기엔 서버·ICE가 필요하다.
여기서는 그 *오케스트레이션*만 fake로 결정적으로 검증한다(slice-07 ingest 더블 관례 확장):
  - 트랙 디스패치: video→consume_video_lk, audio→consume_audio_lk, 그 외(data) 무시 + sid 중복 방지.
  - 늦은 join 순회: 기존 publish track 구독 + 미구독 pub은 set_subscribed(True).
  - poll-clock: 첫 track이 t0 앵커, 타이머가 프레임 없어도 windower.poll(증가하는 now) 구동.
  - worker→loop 브리지: make_signal_bridge(call_soon_threadsafe) → drain → recorder (★ 실 윈도워+
    실 스레드풀로, 워커 스레드 신호가 루프 스레드의 recorder/JSONL까지 도달함을 e2e로 증명).
  - 수명: start=reset(턴 시작), aclose=end_turn(턴 flush)+close+room.disconnect.
  - 토큰/config: env 토큰 우선 → 없으면 self-mint(hidden subscriber grant), 룸 이름 규약.

실행: pytest tests/test_server_subscriber.py
"""
from __future__ import annotations

import asyncio
import json
import threading

import jwt

from giljobe.analysis.mocks import MockCritic, MockTranscriber
from giljobe.emit.sink import JSONLSink
from giljobe.server import config as cfg
from giljobe.server.app import build_subscriber
from giljobe.server.subscriber import (
    KIND_AUDIO,
    KIND_VIDEO,
    LiveKitSubscriber,
    make_signal_bridge,
)
from giljobe.server.token import subscriber_token


# ─────────────────────────── fake LiveKit 표면 ───────────────────────────
class FakeRoom:
    """rtc.Room 대역 — on(데코레이터)/remote_participants/connect/disconnect만 흉내."""

    def __init__(self, remote_participants=None):
        self.handlers: dict[str, list] = {}
        self.remote_participants = remote_participants or {}
        self.connected = False
        self.disconnected = False

    def on(self, event, callback=None):
        def reg(cb):
            self.handlers.setdefault(event, []).append(cb)
            return cb
        return reg(callback) if callback is not None else reg

    def fire(self, event, *args):
        for cb in self.handlers.get(event, []):
            cb(*args)

    async def connect(self, url, token, options):  # noqa: ANN001
        self.connected = True

    def disconnect(self):
        self.disconnected = True


class FakeTrack:
    def __init__(self, kind, sid):
        self.kind, self.sid = kind, sid


class FakePublication:
    def __init__(self, track=None):
        self.track = track
        self.subscribed_calls = []

    def set_subscribed(self, v):
        self.subscribed_calls.append(v)


class FakeParticipant:
    def __init__(self, pubs):
        self.track_publications = pubs


class FakeFrameStream:
    """async-iterable 더블 — VideoFrameEvent/AudioFrameEvent를 yield(consume_*_lk가 소비)."""

    def __init__(self, events):
        self._events = list(events)
        self._i = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev

    async def aclose(self):
        self.closed = True


class _VF:  # VideoFrame 대역
    def __init__(self, w=640, h=480):
        self.width, self.height, self.data = w, h, bytes(w * h * 3)


class _AF:  # AudioFrame 대역
    def __init__(self, samples=320):
        import numpy as np
        self.data = memoryview(np.zeros(samples, dtype=np.int16))
        self.duration = samples / 16000


class _Ev:
    def __init__(self, frame, timestamp_us=None):
        self.frame = frame
        if timestamp_us is not None:
            self.timestamp_us = timestamp_us


class FakeWindower:
    """디스패치/poll/lifecycle 검증용 경량 윈도워 — 호출만 기록(실 추론 없음)."""

    def __init__(self):
        self.frames, self.audio, self.polls, self.ended = [], [], [], []
        self.reset_count = 0
        self.closed = False
        self.on_signal = None

    def add_frame(self, t, j):
        self.frames.append((t, j))

    def add_audio(self, t, p):
        self.audio.append((t, p))

    def poll(self, now):
        self.polls.append(now)

    def end_turn(self, now):
        self.ended.append(now)
        return "TURN_END"

    def reset(self):
        self.reset_count += 1

    def close(self):
        self.closed = True


class FakeRecorder:
    def __init__(self):
        self.signals = []
        self.reset_count = 0

    def on_signal(self, sig):
        self.signals.append(sig)

    def reset(self):
        self.reset_count += 1


def _video_factory(events):
    streams = []

    def make(track, fmt):
        s = FakeFrameStream(events)
        streams.append(s)
        return s
    return make, streams


def _audio_factory(events):
    streams = []

    def make(track, sr, ch):
        s = FakeFrameStream(events)
        streams.append(s)
        return s
    return make, streams


# ─────────────────────────── 1. 트랙 디스패치 ───────────────────────────
def test_track_dispatch_routes_video_and_audio():
    """track_subscribed(video)→consume_video_lk(프레임 투입), (audio)→consume_audio_lk(오디오 투입),
    data/unknown kind는 무시. 첫 track이 poll-clock t0를 앵커한다."""
    async def scenario():
        w = FakeWindower()
        vmake, _ = _video_factory([_Ev(_VF(), timestamp_us=0)])
        amake, _ = _audio_factory([_Ev(_AF())])
        sub = LiveKitSubscriber(FakeRoom(), w, FakeRecorder(),
                                video_stream_factory=vmake, audio_stream_factory=amake)
        await sub.start()
        assert sub._t0 is None
        sub.room.fire("track_subscribed", FakeTrack(KIND_VIDEO, "v1"), None, None)
        assert sub._t0 is not None  # 첫 track이 t0 앵커
        sub.room.fire("track_subscribed", FakeTrack(KIND_AUDIO, "a1"), None, None)
        sub.room.fire("track_subscribed", FakeTrack(999, "d1"), None, None)  # data → 무시
        await asyncio.sleep(0.05)  # consume 태스크 실행
        await sub.aclose(now=1.0)
        return w
    w = asyncio.run(scenario())
    assert len(w.frames) == 1, w.frames     # video 1프레임 투입
    assert len(w.audio) == 1, w.audio       # audio 1청크 투입
    print(f"  ✓ 디스패치: video {len(w.frames)} / audio {len(w.audio)} / data 무시")


def test_track_dispatch_dedups_by_sid():
    """같은 sid의 track_subscribed가 두 번 와도(콜백 ↔ 기존 순회 경합) 한 번만 소비."""
    async def scenario():
        w = FakeWindower()
        vmake, streams = _video_factory([_Ev(_VF(), timestamp_us=0)])
        sub = LiveKitSubscriber(FakeRoom(), w, FakeRecorder(), video_stream_factory=vmake)
        await sub.start()
        sub.room.fire("track_subscribed", FakeTrack(KIND_VIDEO, "v1"), None, None)
        sub.room.fire("track_subscribed", FakeTrack(KIND_VIDEO, "v1"), None, None)  # 중복
        await asyncio.sleep(0.05)
        await sub.aclose(now=1.0)
        return w, streams
    w, streams = asyncio.run(scenario())
    assert len(streams) == 1, "중복 sid는 스트림을 두 번 만들지 않는다"
    assert len(w.frames) == 1
    print("  ✓ sid 중복 디스패치 방지")


# ─────────────────────────── 2. 늦은 join 순회 ───────────────────────────
def test_subscribe_existing_consumes_published_and_requests_unsubscribed():
    """connect 후, 이미 publish된 track은 바로 소비하고, 아직 미구독 pub은 set_subscribed(True)."""
    async def scenario():
        published = FakePublication(track=FakeTrack(KIND_VIDEO, "v-existing"))
        pending = FakePublication(track=None)  # 아직 구독 전
        room = FakeRoom({"cand": FakeParticipant({"p1": published, "p2": pending})})
        w = FakeWindower()
        vmake, streams = _video_factory([_Ev(_VF(), timestamp_us=0)])
        sub = LiveKitSubscriber(room, w, FakeRecorder(), video_stream_factory=vmake)
        await sub.start()
        await sub.connect("ws://x", "tok")
        await asyncio.sleep(0.05)
        await sub.aclose(now=1.0)
        return w, pending, room
    w, pending, room = asyncio.run(scenario())
    assert room.connected
    assert len(w.frames) == 1, "기존 publish track 소비"
    assert pending.subscribed_calls == [True], "미구독 pub은 set_subscribed(True)"
    print("  ✓ 늦은 join: 기존 track 소비 + 미구독 pub 구독 요청")


# ─────────────────────────── 3. poll-clock 타이머 ───────────────────────────
def test_poll_loop_advances_during_silence():
    """프레임이 끊겨도(추가 add_frame 없음) 타이머가 windower.poll(증가하는 now>0)를 계속 구동."""
    async def scenario():
        w = FakeWindower()
        sub = LiveKitSubscriber(FakeRoom(), w, FakeRecorder(), poll_interval=0.02)
        await sub.start()
        sub._t0 = sub._loop.time()  # 첫 track 앵커 모사(프레임은 안 넣음 = 침묵)
        await asyncio.sleep(0.15)   # ~7틱
        await sub.aclose(now=1.0)
        return w
    w = asyncio.run(scenario())
    assert len(w.polls) >= 3, w.polls               # 침묵에도 여러 번 poll
    assert all(n > 0 for n in w.polls)
    assert w.polls == sorted(w.polls)               # now 단조증가
    print(f"  ✓ 침묵 중 poll {len(w.polls)}회, now 단조증가 {[round(n,3) for n in w.polls]}")


# ─────────────────────────── 4. 수명(reset/end_turn/close) ───────────────────────────
def test_lifecycle_start_resets_and_aclose_ends_turn():
    """start=윈도워+recorder reset(턴 시작), aclose=end_turn(턴 flush)+close+room.disconnect."""
    async def scenario():
        w, rec, room = FakeWindower(), FakeRecorder(), FakeRoom()
        sub = LiveKitSubscriber(room, w, rec)
        await sub.start()
        assert w.reset_count == 1 and rec.reset_count == 1  # 턴 시작
        await sub.aclose(now=7.5)
        return w, room
    w, room = asyncio.run(scenario())
    assert w.ended == [7.5], "aclose가 end_turn(now) 호출"
    assert w.closed and room.disconnected
    print("  ✓ 수명: start=reset / aclose=end_turn(7.5)+close+disconnect")


def test_aclose_is_idempotent():
    """aclose 중복 호출은 안전(멱등) — end_turn 두 번 돌지 않음."""
    async def scenario():
        w = FakeWindower()
        sub = LiveKitSubscriber(FakeRoom(), w, FakeRecorder())
        await sub.start()
        await sub.aclose(now=3.0)
        await sub.aclose(now=3.0)  # 두 번째는 no-op
        return w
    w = asyncio.run(scenario())
    assert w.ended == [3.0], w.ended
    print("  ✓ aclose 멱등(end_turn 1회)")


def test_late_track_after_aclose_is_ignored():
    """aclose 후 늦게 온 track_subscribed는 무시 — orphan 소비 태스크가 생기지 않는다(_started 가드)."""
    async def scenario():
        w = FakeWindower()
        vmake, streams = _video_factory([_Ev(_VF(), timestamp_us=0)])
        sub = LiveKitSubscriber(FakeRoom(), w, FakeRecorder(), video_stream_factory=vmake)
        await sub.start()
        await sub.aclose(now=1.0)
        sub.room.fire("track_subscribed", FakeTrack(KIND_VIDEO, "late"), None, None)  # 종료 후 늦은 track
        await asyncio.sleep(0.02)
        return streams, w
    streams, w = asyncio.run(scenario())
    assert streams == [], "aclose 후 track은 스트림(소비 태스크)을 만들지 않는다"
    assert w.frames == []
    print("  ✓ aclose 후 늦은 track 무시(orphan 소비 태스크 방지)")


# ─────────────────────────── 5. worker→loop 브리지 (단위) ───────────────────────────
def test_signal_bridge_marshals_from_worker_thread():
    """make_signal_bridge: 워커 스레드에서 부른 on_signal이 call_soon_threadsafe로 루프 큐에 안착."""
    async def scenario():
        q = asyncio.Queue()
        bridge = make_signal_bridge(asyncio.get_running_loop(), q)
        threading.Thread(target=lambda: bridge("S1")).start()
        await asyncio.sleep(0.05)  # 스케줄된 put_nowait 실행
        return q.get_nowait()
    assert asyncio.run(scenario()) == "S1"
    print("  ✓ 브리지: 워커 스레드 신호 → 루프 큐")


# ─────────────── 6. 실 윈도워+스레드풀 브리지 e2e → JSONL (★ load-bearing) ───────────────
def test_real_windower_bridge_to_jsonl(tmp_path):
    """★ 서버 브리지(call_soon_threadsafe→drain→recorder)를 실 TurnWindower+실 스레드풀+실 recorder/
    JSONLSink로 검증: 합성 9s 입력이 워커 스레드 → 루프 스레드 → JSONL까지 window 3 + turn_end 1로 도달.
    (test_e2e_mock은 on_signal=recorder 직결; 여기선 그 사이에 *서버 브리지 레이어*가 끼어도 무손실임을 증명.)"""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    config = cfg.SubscriberConfig(url="ws://x", room="r", session_id="iv-1", poll_interval=0.05)
    sub = build_subscriber(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        [jsonl],
        config,
        room=FakeRoom(),
    )

    async def scenario():
        await sub.start()
        w = sub.windower
        t = 0.0
        while t < 9.0:
            w.add_frame(t, b"jpeg"); t += 1.0
        t = 0.0
        while t < 9.0:
            w.add_audio(t, b"pcm"); t += 0.5
        w.poll(9.0)
        await asyncio.sleep(0.4)   # 워커 완료 + 드레인이 window 레코드 흘릴 시간
        await sub.aclose(now=9.0)  # end_turn → turn_end + flush
    try:
        asyncio.run(scenario())
    finally:
        jsonl.close()

    parsed = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    windows = [r for r in parsed if r["type"] == "window"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]
    assert len(windows) == 3, [r["t"] for r in windows]
    assert sorted(r["t"] for r in windows) == [1.5, 4.5, 7.5]
    assert all(r["transcript"] == "조각" and r["nonverbal"]["state"] == "attentive" for r in windows)
    assert len(turn_ends) == 1 and parsed[-1]["type"] == "turn_end"
    assert turn_ends[0]["transcript_full"] == "조각 조각 조각"
    print(f"  ✓ 실 브리지 e2e → JSONL: window {len(windows)} + turn_end {len(turn_ends)}")


# ─────────────────────────── 7. 토큰 / config ───────────────────────────
def test_subscriber_token_is_hidden_subscriber_grant():
    """self-mint 토큰은 hidden + can_subscribe + can_publish=False (분석 봇 grant)."""
    tok = subscriber_token("devkey", "mysecret", "giljob-session-x", identity="giljobe-analysis")
    dec = jwt.decode(tok, "mysecret", algorithms=["HS256"])
    g = dec["video"]
    assert g["roomJoin"] and g["room"] == "giljob-session-x"
    assert g["canSubscribe"] and g["canPublish"] is False and g["hidden"] is True
    assert dec["sub"] == "giljobe-analysis"
    print("  ✓ 토큰: hidden subscriber grant(canPublish=False)")


def test_resolve_token_prefers_env_then_mints(monkeypatch):
    """resolve_token: LIVEKIT_TOKEN 있으면 그대로, 없고 키 있으면 self-mint, 둘 다 없으면 None."""
    for k in ("LIVEKIT_TOKEN", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.resolve_token("giljob-session-x") is None  # 아무것도 없음

    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "mysecret")
    minted = cfg.resolve_token("giljob-session-x")
    assert minted and jwt.decode(minted, "mysecret", algorithms=["HS256"])["video"]["hidden"] is True

    monkeypatch.setenv("LIVEKIT_TOKEN", "PRE_ISSUED")
    assert cfg.resolve_token("giljob-session-x") == "PRE_ISSUED"  # env 토큰 우선
    print("  ✓ resolve_token: none→mint→env 우선순위")


def test_room_name_convention():
    assert cfg.room_name("abc123") == "giljob-session-abc123"
    print("  ✓ 룸 이름 규약 giljob-session-{id}")


def main() -> int:
    import inspect as _inspect
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== server subscriber tests ({len(tests)}) ===")
    failed = 0
    for t in tests:
        try:
            # monkeypatch/ tmp_path 필요한 건 pytest로만 — standalone에선 skip
            params = _inspect.signature(t).parameters
            if params:
                print(f"  ~ {t.__name__}: (pytest fixture 필요 — standalone skip)")
                continue
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
    print(f"=== {'ALL PASS' if not failed else f'{failed} FAILED'} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
