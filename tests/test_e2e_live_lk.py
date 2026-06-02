#!/usr/bin/env python3
"""실 LiveKit 라이브 e2e — 진짜 LiveKit 서버에 publish→subscribe 전 경로 (서버 down이면 skip).

오프라인 테스트(test_ingest_lk/test_server_subscriber)는 더블로 *오케스트레이션*만 본다. 여기서는
**실 LiveKit 서버**에 publisher(candidate 모사)가 RGB24 영상+16k mono 오디오를 올리고, 우리
hidden subscriber가 같은 룸에 붙어 ingest→windower→recorder→JSONL까지 가는 전 경로를 실증한다.
이게 검증하는 것: 실 RGB24 프레임으로 encode_jpeg_rgb(stride 가정 포함) · 실 16k mono 직수신 ·
LiveKit 구독/track_subscribed 배선 · ICE 미디어 전송 · worker→loop 브리지. (critic은 Mock — 종단
모델 품질·레이턴시는 Slice 9 실 critic 몫.)

게이트(아래 중 하나라도 불가 → skip, gje test_e2e_pipeline 관례):
  - LIVEKIT_API_KEY/SECRET (env 우선; 없으면 로컬 giljob-v2 api 컨테이너 env에서 읽음 — 인가됨,
    값 노출 안 함) · LiveKit 서버(localhost:7880) 도달 가능.

실행: pytest tests/test_e2e_live_lk.py -v -s   (서버 떠 있을 때만 실제 수행)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from collections import Counter
from datetime import timedelta

import pytest

LIVEKIT_HOST_URL = "ws://localhost:7880"
DUR = 7.0  # publish 길이(초) — 윈도우 2~3개 생성


def _read_container_keys() -> tuple[str, str] | None:
    """로컬 giljob-v2 api 컨테이너 env에서 LiveKit 키를 읽는다(사용자 인가; 값 로그/반환만, 출력 안 함)."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "giljob-v2-api-1",
             "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        env = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        key, secret = env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET")
        return (key, secret) if key and secret else None
    except Exception:  # noqa: BLE001 - docker 없음/타임아웃 등 → 게이트 미충족(skip)
        return None


def _live_creds() -> tuple[str, str, str] | None:
    """(url, key, secret) 또는 None. env 우선, 없으면 컨테이너. 서버 미도달이면 None."""
    key, secret = os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    url = os.environ.get("LIVEKIT_URL") or LIVEKIT_HOST_URL
    if not (key and secret):
        ks = _read_container_keys()
        if ks is None:
            return None
        key, secret = ks
        url = LIVEKIT_HOST_URL  # 컨테이너 LIVEKIT_URL은 내부망(host에선 localhost로)
    # 서버 도달성(7880 TCP) 빠른 확인
    host = url.split("://", 1)[-1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname or "localhost", int(port or 7880)), timeout=2):
            pass
    except OSError:
        return None
    return url, key, secret


def _pub_token(key: str, secret: str, room: str) -> str:
    from livekit import api

    return (
        api.AccessToken(key, secret)
        .with_identity("candidate-sim")
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=False))
        .with_ttl(timedelta(seconds=600))
        .to_jwt()
    )


async def _publish_synthetic_media(url: str, token: str, stop: asyncio.Event) -> None:
    """candidate 모사 publisher — RGB24 640×480 영상(10fps) + 16k mono 오디오(~realtime)를 stop까지 송출."""
    from livekit import rtc

    room = rtc.Room()
    await room.connect(url, token, rtc.RoomOptions(auto_subscribe=False))
    vsrc = rtc.VideoSource(640, 480)
    asrc = rtc.AudioSource(16000, 1)
    await room.local_participant.publish_track(
        rtc.LocalVideoTrack.create_video_track("cam", vsrc),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
    )
    await room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("mic", asrc),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    rgb = bytes([128]) * (640 * 480 * 3)
    pcm = (b"\x10\x00") * 320  # 320 samples mono int16(=20ms@16k)

    async def push_video() -> None:
        i = 0
        while not stop.is_set():
            vsrc.capture_frame(
                rtc.VideoFrame(640, 480, rtc.VideoBufferType.RGB24, rgb), timestamp_us=i * 100_000
            )
            i += 1
            await asyncio.sleep(0.1)

    async def push_audio() -> None:
        while not stop.is_set():
            await asrc.capture_frame(rtc.AudioFrame(pcm, 16000, 1, 320))  # ~realtime 페이싱

    try:
        await asyncio.gather(push_video(), push_audio())
    finally:
        await room.disconnect()


def test_live_publish_subscribe_to_jsonl(tmp_path):
    """실 LiveKit: publisher 미디어 → hidden subscriber → JSONL에 window+turn_end 레코드가 떨어진다."""
    creds = _live_creds()
    if creds is None:
        pytest.skip("LiveKit 서버/키 미가용 (env 또는 로컬 giljob-v2) — 라이브 e2e skip")
    url, key, secret = creds

    from giljobe.analysis.mocks import MockCritic, MockTranscriber
    from giljobe.emit.sink import JSONLSink
    from giljobe.server.app import build_subscriber
    from giljobe.server.config import SubscriberConfig
    from giljobe.server.token import subscriber_token

    room = f"giljobe-slice8-e2e-{os.getpid()}"
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    config = SubscriberConfig(
        url=url, room=room, session_id="e2e", poll_interval=0.5,
        token=subscriber_token(key, secret, room),
    )

    async def scenario() -> None:
        # ★ rtc.Room()은 실행 중인 루프 안에서 생성해야 한다 — 동기 본문에서 만들면 다른 루프에
        #   바인딩돼 disconnect 시 "attached to a different loop"로 깨진다(spike에서 확인).
        sub = build_subscriber(
            MockCritic(transcriber=MockTranscriber(text="라이브")), [jsonl], config,
        )
        await sub.start()
        await asyncio.wait_for(sub.connect(url, config.token), timeout=15)
        stop = asyncio.Event()
        pub = asyncio.create_task(_publish_synthetic_media(url, _pub_token(key, secret, room), stop))
        try:
            await asyncio.sleep(DUR)
        finally:
            stop.set()
            await asyncio.sleep(0.3)
            await sub.aclose(now=DUR)
            pub.cancel()
            try:
                await pub
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=DUR + 30))
    finally:
        jsonl.close()

    parsed = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    types = Counter(r["type"] for r in parsed)
    windows = [r for r in parsed if r["type"] == "window"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]

    # 실 미디어가 실제로 흘러 윈도우가 생성됐는가(ICE/구독/ingest 전 경로).
    assert len(windows) >= 2, f"실 미디어로 윈도우 ≥2 기대, got {types} (ICE/구독 실패 가능)"
    assert all(w["nonverbal"]["state"] == "attentive" for w in windows), "nv 병합(영상 도착)"
    assert all(w["transcript"] == "라이브" for w in windows), "전사 병합(오디오 도착)"
    assert len(turn_ends) == 1 and parsed[-1]["type"] == "turn_end", "턴 종료 1개, 파일 마지막"
    assert "라이브" in turn_ends[0]["transcript_full"], turn_ends[0]["transcript_full"]
    print(f"  ✓ 실 LiveKit e2e: {dict(types)}, window ts={sorted(w['t'] for w in windows)}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
