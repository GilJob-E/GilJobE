#!/usr/bin/env python3
"""★ Slice 9 라이브 e2e — 실 LiveKit + **실 critic**(vLLM E4B) 종단 측정.

Slice 8의 라이브 e2e(test_e2e_live_lk)는 MockCritic으로 *전송·배선*만 봤다. 여기서는 같은
LiveKit 구독 경로에 **실 WindowCritic + GemmaNativeTranscriber**(같은 vLLM E4B)를 배선해
실제 한국어 미디어(kor.mp4)를 흘리고 종단 품질·지연을 잰다. 측정 4종(NEXT.md Slice 9 §4):
  1. 종단 레이턴시(freshness lag): 윈도우 미디어 구간이 끝난 시각 → 그 window 레코드 emit 시각.
  2. per-window 비언어 read 품질(state/intensity/note) — 사람이 .json으로 루브릭 판정.
  3. 전사 품질(한국어 verbatim·환각) — per-window transcript + turn_end transcript_full.
  4. eot finalize — turn_end 레코드(transcript_full + compact eval).
  + deferred #1: 크로스트랙 t0 — 첫 video vs 첫 audio 프레임 도착 시차(skew) 측정.

입력 경로(사용자 결정 = 옵션 1): **독립 livekit-server:v1.8(dev)** 에 candidate 모사 publisher가
kor.mp4(실 한국어 자기소개 + 얼굴)를 RGB24 영상 + 16k mono 오디오로 realtime push → 우리 hidden
subscriber가 같은 룸에 붙어 ingest→windower→real critic→JSONL. publisher/subscriber/critic이 한
이벤트루프에서 도는데, critic은 윈도워 executor(스레드풀)에서 돌고 publisher는 사전 디코드분만
realtime으로 흘리므로 이벤트루프를 막지 않는다.

게이트(둘 중 하나라도 불가 → skip, gje test_e2e_pipeline 관례):
  - vLLM health(client.health()) — 다운이면 skip(`bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`).
  - LiveKit 서버 도달(localhost:7880). dev 모드 키(devkey/secret) — 통합 giljob-v2 키 불요.
  - kor.mp4(repo 루트, gitignore) 존재.

측정치는 `tests/evidence/live-critic.raw.json`에 기록(판정·루브릭은 사람이 .json으로 큐레이션).
재현: 독립 livekit 기동 후 `.venv/bin/python -m pytest tests/test_e2e_live_critic.py -v -s`
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

LIVEKIT_URL = "ws://localhost:7880"
DEV_KEY = "devkey"      # livekit-server --dev 기본 키(플레이스홀더, 비밀 아님)
DEV_SECRET = "secret"   # 통합 타깃 키가 아니라 독립 dev 서버 전용

CLIP = Path(__file__).parent.parent / "kor.mp4"
EVIDENCE = Path(__file__).parent / "evidence" / "live-critic.raw.json"

PUB_FPS = 10.0          # publisher 송출 fps(subscriber는 1fps throttle — 트랙 유지용 여유)
PUB_W, PUB_H = 360, 640  # 송출 해상도(subscriber가 640x480으로 다시 리사이즈) — 대역폭·메모리 절감
POLL_INTERVAL = 0.5
EOT_DEADLINE = 8.0      # 실 모델 compact tail(~2–3s)/최종 stt를 기다리는 backstop(Mock 3s보다 여유)
SAMPLE_RATE = 16000


# ── 게이트 ──
def _livekit_up() -> bool:
    host = LIVEKIT_URL.split("://", 1)[-1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname or "localhost", int(port or 7880)), timeout=2):
            return True
    except OSError:
        return False


# ── kor.mp4 사전 디코드(이벤트루프 밖에서 미리 — realtime push 중 디코드 지터/블로킹 방지) ──
def _decode_kor(path: Path, *, fps: float, size: tuple[int, int]):
    """kor.mp4 → (video_frames, audio_pcm).
    video_frames=[(media_t_s, rgb24_bytes)] (size=(w,h), ~fps), audio_pcm=16k mono Int16 전체 버퍼."""
    import av

    container = av.open(str(path))
    v = container.streams.video[0]
    frames: list[tuple[float, bytes]] = []
    interval = 1.0 / fps
    next_t = 0.0
    for frame in container.decode(v):
        if frame.pts is None:
            continue
        mt = float(frame.pts * v.time_base)
        if mt + 1e-6 < next_t:
            continue
        next_t = mt + interval
        rgb = frame.reformat(width=size[0], height=size[1], format="rgb24").to_ndarray()
        frames.append((mt, rgb.tobytes()))  # (h, w, 3) row-major = tight RGB24
    container.close()

    container = av.open(str(path))
    a = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks: list[bytes] = []
    for frame in container.decode(a):
        for rs in resampler.resample(frame):
            chunks.append(rs.to_ndarray().tobytes())
    for rs in resampler.resample(None):  # 꼬리 flush
        chunks.append(rs.to_ndarray().tobytes())
    container.close()
    return frames, b"".join(chunks)


def _pub_token(room: str) -> str:
    from livekit import api

    return (
        api.AccessToken(DEV_KEY, DEV_SECRET)
        .with_identity("candidate-sim")
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=False))
        .with_ttl(timedelta(seconds=600))
        .to_jwt()
    )


async def _publish_kor(url: str, token: str, frames, audio_pcm: bytes, stop: asyncio.Event) -> None:
    """candidate 모사 — kor.mp4 프레임(RGB24)+오디오(16k mono)를 realtime으로 송출(stop까지)."""
    from livekit import rtc

    loop = asyncio.get_running_loop()
    room = rtc.Room()
    await room.connect(url, token, rtc.RoomOptions(auto_subscribe=False))
    vsrc = rtc.VideoSource(PUB_W, PUB_H)
    asrc = rtc.AudioSource(SAMPLE_RATE, 1)
    await room.local_participant.publish_track(
        rtc.LocalVideoTrack.create_video_track("cam", vsrc),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
    )
    await room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("mic", asrc),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    async def push_video() -> None:
        start = loop.time()
        for (mt, rgb) in frames:
            if stop.is_set():
                return
            ahead = mt - (loop.time() - start)
            if ahead > 0:
                await asyncio.sleep(ahead)  # realtime pace(미디어시각 = 경과 wall)
            vsrc.capture_frame(
                rtc.VideoFrame(PUB_W, PUB_H, rtc.VideoBufferType.RGB24, rgb),
                timestamp_us=int(mt * 1e6),
            )

    async def push_audio() -> None:
        step = 320 * 2  # 20ms @16k mono Int16 = 320 samples = 640 bytes
        for i in range(0, len(audio_pcm), step):
            if stop.is_set():
                return
            chunk = audio_pcm[i : i + step]
            n = len(chunk) // 2
            if n == 0:
                break
            await asrc.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, n))  # AudioSource가 realtime 페이싱

    try:
        await asyncio.gather(push_video(), push_audio())
    finally:
        await room.disconnect()


# ── 계측(src 무수정) ──
class _ClockSink:
    """레코드 emit 시각(loop.time)을 찍는 Sink — recorder fan-out이 루프 스레드(drain)에서 부르므로
    loop.time()이 유효. freshness lag 계산용(window 레코드의 emit 시각 ↔ 미디어 구간 종료 시각)."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.entries: list[tuple[float, dict]] = []  # (emit_loop_t, record)

    def emit(self, record: dict) -> None:
        self.entries.append((self.clock(), record))


class _InstWindower:
    """윈도워 위임 프록시 — 첫 add_frame/add_audio 호출 시각(loop.time)을 찍어 크로스트랙 t0 skew 측정.
    on_signal 설정/조회는 inner로 라우팅(윈도워 _emit이 inner.on_signal을 읽으므로)."""

    def __init__(self, inner, clock) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "first_video_t", None)
        object.__setattr__(self, "first_audio_t", None)

    def add_frame(self, t_s: float, jpeg: bytes) -> None:
        if self.first_video_t is None:
            object.__setattr__(self, "first_video_t", self._clock())
        self._inner.add_frame(t_s, jpeg)

    def add_audio(self, t_s: float, pcm: bytes) -> None:
        if self.first_audio_t is None:
            object.__setattr__(self, "first_audio_t", self._clock())
        self._inner.add_audio(t_s, pcm)

    def __getattr__(self, name):  # reset/poll/end_turn/close/on_signal 등 → inner
        return getattr(self._inner, name)

    def __setattr__(self, name, value):  # 외부에서 오는 건 on_signal뿐 → inner로
        setattr(self._inner, name, value)


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def test_e2e_live_critic(tmp_path):
    """실 LiveKit + 실 critic으로 kor.mp4를 종단 처리하고 레이턴시·전사·비언어·crosstrack을 evidence로 남긴다."""
    from giljobe.analysis.critic import WindowCritic
    from giljobe.emit.sink import JSONLSink, SignalRecorder
    from giljobe.llm.vllm_client import default_vllm_client
    from giljobe.pipeline.windowing import TurnWindower
    from giljobe.server.subscriber import LiveKitSubscriber
    from giljobe.server.token import subscriber_token

    client = default_vllm_client()
    try:
        client.health()
    except Exception as e:  # noqa: BLE001 - 다운이면 라이브 검증 불가 → skip
        pytest.skip(f"vLLM down ({client.base_url}): {e}")
    if not _livekit_up():
        pytest.skip(f"LiveKit 서버 미도달 ({LIVEKIT_URL}) — 독립 livekit-server 기동 후 재시도")
    if not CLIP.exists():
        pytest.skip(f"clip missing: {CLIP}")

    frames, audio_pcm = _decode_kor(CLIP, fps=PUB_FPS, size=(PUB_W, PUB_H))
    media_dur = max(frames[-1][0], len(audio_pcm) / 2 / SAMPLE_RATE)
    room = f"giljob-session-slice9-{os.getpid()}"
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    sub_token = subscriber_token(DEV_KEY, DEV_SECRET, room)

    state: dict = {}

    async def scenario() -> None:
        from livekit import rtc

        loop = asyncio.get_running_loop()
        critic = WindowCritic(client=client)  # 실 critic, 기본 transcriber=GemmaNative(같은 client)
        await loop.run_in_executor(None, critic.warmup)  # 콜드 그래프 선warm(측정 밖으로)

        clock = _ClockSink(loop.time)
        recorder = SignalRecorder("slice9", [jsonl, clock])
        windower = TurnWindower(critic, eot_deadline_s=EOT_DEADLINE)
        inst = _InstWindower(windower, loop.time)
        sub = LiveKitSubscriber(rtc.Room(), inst, recorder, poll_interval=POLL_INTERVAL)

        await sub.start()
        await asyncio.wait_for(sub.connect(LIVEKIT_URL, sub_token), timeout=15)
        stop = asyncio.Event()
        pub = asyncio.create_task(_publish_kor(LIVEKIT_URL, _pub_token(room), frames, audio_pcm, stop))
        try:
            await asyncio.sleep(media_dur + 1.5)  # 클립 전체 스트리밍 + 소량 여유
        finally:
            stop.set()
            await asyncio.sleep(0.3)
            state["aclose_at"] = loop.time()
            await sub.aclose()  # now=None → _media_now(); end_turn flush + 정리
            pub.cancel()
            try:
                await pub
            except asyncio.CancelledError:
                pass
        state["t0"] = sub._t0
        state["first_video_t"] = inst.first_video_t
        state["first_audio_t"] = inst.first_audio_t
        state["clock_entries"] = clock.entries

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=media_dur + 90))
    finally:
        jsonl.close()

    parsed = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    types = Counter(r["type"] for r in parsed)
    windows = [r for r in parsed if r["type"] == "window"]
    evals = [r for r in parsed if r["type"] == "eval"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]

    # ── 레이턴시(freshness lag): emit 시각 ↔ 윈도우 미디어 구간 종료 시각 ──
    # clock entries(emit 시각 + 그 *원본* 레코드 객체)를 측정의 진실원으로 쓴다. JSONL 재파싱은
    # 새 dict라 emit 시각과 못 잇는다(파일은 counts/순서 sanity용으로만).
    t0, aclose_at = state["t0"], state.get("aclose_at")
    live_lat, eot_lat = [], []
    win_rows = []
    for (te, r) in state["clock_entries"]:
        if r["type"] != "window":
            continue
        media_end = r["t"] + r["window_s"] / 2.0  # t=윈도우 중심 → 종료=t+window_s/2
        lag = None if t0 is None else round(te - (t0 + media_end), 3)
        is_eot = aclose_at is not None and te >= aclose_at
        if lag is not None:
            (eot_lat if is_eot else live_lat).append(lag)
        win_rows.append({
            "t": r["t"], "window_s": r["window_s"],
            "nonverbal": r["nonverbal"], "transcript": r["transcript"],
            "transcript_final": r["transcript_final"],
            "freshness_lag_s": lag, "flushed_at_eot": is_eot,
        })

    def _stats(xs):
        xs = [x for x in xs if x is not None]
        if not xs:
            return None
        return {"n": len(xs), "min": round(min(xs), 3),
                "median": round(statistics.median(xs), 3), "max": round(max(xs), 3)}

    # ── 크로스트랙 t0 skew(deferred #1) ──
    fv, fa = state["first_video_t"], state["first_audio_t"]
    crosstrack = {
        "first_video_after_t0_s": None if (fv is None or t0 is None) else round(fv - t0, 3),
        "first_audio_after_t0_s": None if (fa is None or t0 is None) else round(fa - t0, 3),
        "skew_audio_minus_video_s": None if (fv is None or fa is None) else round(fa - fv, 3),
        "note": ("파일 publisher(두 트랙 동시 송출) → LiveKit 구독/네고 skew의 하한. "
                 "실 브라우저 getUserMedia는 디바이스 기동 skew가 더해져 더 클 수 있음."),
    }

    transcript_full = turn_ends[0]["transcript_full"] if turn_ends else ""
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps({
        "slice": 9,
        "check": "e2e-live-critic (LiveKit subscribe → 실 WindowCritic+GemmaNativeTranscriber → JSONL)",
        "model": critic_model(),
        "transport": "독립 livekit-server:v1.8 (dev, --network host, localhost ICE)",
        "input": f"kor.mp4 (publisher {PUB_W}x{PUB_H} RGB24 @{PUB_FPS:g}fps + 16k mono, realtime)",
        "publish_media_dur_s": round(media_dur, 2),
        "params": {"poll_interval": POLL_INTERVAL, "eot_deadline_s": EOT_DEADLINE, "nv_grid_s": 3.0},
        "counts": dict(types),
        "latency_freshness_lag": {
            "desc": "emit 시각 − (t0 + 윈도우 미디어구간 종료). poll지연(≤poll_interval) + critic 추론 포함.",
            "live_windows": _stats(live_lat),
            "eot_flushed_windows": _stats(eot_lat),
        },
        "crosstrack_t0": crosstrack,
        "windows": win_rows,
        "evals": [{"t": e["t"], "window_s": e["window_s"], "eval": e["eval"]} for e in evals],
        "turn_end": {
            "transcript_full": transcript_full,
            "transcript_full_chars": len(transcript_full),
            "transcript_full_hangul": _has_hangul(transcript_full),
            "eval": turn_ends[0]["eval"] if turn_ends else None,
        },
        "verdict": "사람이 .json으로 큐레이션(루브릭 판정)",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 콘솔 요약(사람 루브릭 판정용)
    print(f"\n=== Slice 9 라이브 e2e (실 critic) ===  counts={dict(types)}")
    print(f"freshness lag(live)={_stats(live_lat)}  crosstrack={crosstrack['skew_audio_minus_video_s']}s")
    for w in win_rows:
        nv = w["nonverbal"] or {}
        print(f"  t={w['t']:>5} nv={nv.get('state','-')}/{nv.get('intensity','-')} "
              f"lag={w['freshness_lag_s']}s tx={w['transcript']!r}")
    print(f"  transcript_full({len(transcript_full)}자)={transcript_full!r}")
    if turn_ends:
        print(f"  turn_end.eval={turn_ends[0]['eval']}")

    # ── 배선·흐름 sanity(품질은 사람 판정, 여기선 종단 흐름만 보증) ──
    assert len(windows) >= 5, f"실 미디어로 window ≥5 기대, got {dict(types)}"
    assert any(w["nonverbal"] is not None for w in windows), "nv 병합(영상 도착) 0건"
    assert sum(1 for w in windows if _has_hangul(w["transcript"])) >= 3, "한글 전사 윈도우 ≥3 기대"
    assert len(turn_ends) == 1 and parsed[-1]["type"] == "turn_end", "turn_end 1개, 파일 마지막"
    assert _has_hangul(transcript_full), f"transcript_full 한글 아님: {transcript_full!r}"
    assert live_lat, "freshness lag 측정치 0건(계측 실패)"


def critic_model() -> str:
    from giljobe.analysis.critic import MODEL

    return MODEL


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
