#!/usr/bin/env python3
"""LiveKit ingest glue 단위 테스트 — async-iterable 더블로 consume_*_lk 직격 (livekit/GPU 불요).

핵심 커버(NEXT.md Slice 8 §4 검증 항목 + `.dev/livekit-sdk-research.md` 실측 매핑):
  - encode_jpeg_rgb: RGB24 raw 버퍼(+stride 패딩) → 640×480 JPEG.
  - video: ev.timestamp_us/1e6 첫프레임 t0 정규화 + 1fps throttle(누적 드리프트 없음).
  - audio: 누적 frame.duration 미디어시계(첫 청크 t=0) + PCM bytes 무변경 passthrough(리샘플 없음).
  - 종료=EOS(async for 자연 종료) 시 stream.aclose() 호출, 취소(CancelledError)는 전파.

consume_*_lk는 livekit을 import하지 않고 이벤트를 덕타이핑(.frame/.timestamp_us/.duration/.data)으로만
다루므로, 실 SDK 없이 더블로 전 경로를 검증할 수 있다(실 stream 생성은 server 레이어 책임).

실행: pytest tests/test_ingest_lk.py
      (standalone: PYTHONPATH=src python3 tests/test_ingest_lk.py)
"""
from __future__ import annotations

import asyncio
import io

import numpy as np
from PIL import Image

from giljobe.media import ingest

SR = ingest.SAMPLE_RATE  # 16000 (LiveKit AudioStream이 직접 공급하는 레이트)


# ── async-iterable 더블 (rtc.VideoStream/AudioStream 대역) ──
class FakeVideoFrame:
    def __init__(self, width: int, height: int, data: bytes):
        self.width, self.height, self.data = width, height, data


class FakeAudioFrame:
    def __init__(self, data, duration: float):
        self.data, self.duration = data, duration


class FakeEvent:
    """VideoFrameEvent(frame, timestamp_us) / AudioFrameEvent(frame)를 겸한 더블."""

    def __init__(self, frame, timestamp_us: int | None = None):
        self.frame = frame
        if timestamp_us is not None:
            self.timestamp_us = timestamp_us


class FakeStream:
    """이벤트 리스트를 순서대로 내고 소진되면 EOS(StopAsyncIteration). aclose 호출 여부를 기록."""

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


class CancellingStream:
    """첫 __anext__에서 CancelledError(태스크 취소=연결 해제/턴 종료) — 전파돼야 한다."""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise asyncio.CancelledError

    async def aclose(self):
        self.closed = True


class RecordingWindower:
    """_Windower 표면 더블 — add_frame/add_audio 호출을 (t, payload)로 기록."""

    def __init__(self):
        self.frames: list[tuple[float, bytes]] = []
        self.audio: list[tuple[float, bytes]] = []

    def add_frame(self, t_s: float, jpeg: bytes) -> None:
        self.frames.append((t_s, jpeg))

    def add_audio(self, t_s: float, pcm: bytes) -> None:
        self.audio.append((t_s, pcm))


# ── 합성 빌더 ──
def rgb24_frame(idx: int, *, w: int = 1280, h: int = 720, stride_pad: int = 0) -> FakeVideoFrame:
    """tight RGB24(stride_pad=0) 또는 행 stride 패딩(>0)을 가진 버퍼. 픽셀값=idx로 채워 구분."""
    if stride_pad == 0:
        data = bytes([idx % 256]) * (w * h * 3)
    else:  # 각 행: 유효 w*3 + 패딩 stride_pad
        row = bytes([idx % 256]) * (w * 3) + b"\x00" * stride_pad
        data = row * h
    return FakeVideoFrame(w, h, data)


def video_events(n: int, *, fps: int = 30, t0_us: int = 0):
    """n개 VideoFrameEvent를 1/fps 간격 timestamp_us로(첫값=t0_us). round로 누적오차 없이 — 30fps면
    i=30이 정확히 1.0s에 떨어져 1fps throttle 결과가 깔끔(실 us 타임스탬프의 정수 마이크로초 모사)."""
    return [
        FakeEvent(rgb24_frame(i, w=640, h=480), timestamp_us=t0_us + round(i * 1_000_000 / fps))
        for i in range(n)
    ]


def audio_event(idx: int, *, samples: int = 320) -> FakeEvent:
    """16k mono Int16 청크(memoryview) — duration=samples/16000. 값=idx로 채워 passthrough 확인."""
    arr = np.full(samples, idx % 1000, dtype=np.int16)
    return FakeEvent(FakeAudioFrame(memoryview(arr), samples / SR))


# ── encode_jpeg_rgb ──
def test_encode_jpeg_rgb_tight_buffer():
    """tight RGB24(width*height*3) → 640×480 JPEG로 인코드(디코드 라운드트립)."""
    f = rgb24_frame(7, w=1280, h=720)
    jpeg = ingest.encode_jpeg_rgb(f.data, f.width, f.height)
    assert jpeg[:3] == b"\xff\xd8\xff", "JPEG SOI 매직"
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (ingest.VIDEO_WIDTH, ingest.VIDEO_HEIGHT) and img.format == "JPEG"
    print(f"  ✓ tight RGB24 1280×720 → JPEG {len(jpeg)}B size={img.size}")


def test_encode_jpeg_rgb_handles_row_stride():
    """행 stride 패딩이 있는 RGB24 버퍼도 패딩을 떼고 640×480 JPEG로(크래시·왜곡 없음)."""
    f = rgb24_frame(9, w=640, h=480, stride_pad=12)  # 행마다 12바이트 패딩
    jpeg = ingest.encode_jpeg_rgb(f.data, f.width, f.height)
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (ingest.VIDEO_WIDTH, ingest.VIDEO_HEIGHT) and img.format == "JPEG"
    print(f"  ✓ stride 패딩 흡수 → JPEG {len(jpeg)}B size={img.size}")


# ── consume_video_lk ──
def test_consume_video_lk_throttles_to_1fps():
    """30fps 입력 2.1s → 1fps throttle로 t≈0,1,2 세 프레임만(나머지 드롭, 전부 640×480)."""
    rec = RecordingWindower()
    asyncio.run(ingest.consume_video_lk(FakeStream(video_events(64, fps=30)), rec))
    times = [t for (t, _) in rec.frames]
    assert len(times) == 3, times
    for got, want in zip(times, (0.0, 1.0, 2.0)):
        assert abs(got - want) < 1e-6, (got, want)
    for _, jpeg in rec.frames:
        assert Image.open(io.BytesIO(jpeg)).size == (640, 480)
    print(f"  ✓ 64ev@30fps → 3프레임@1fps t={times}")


def test_consume_video_lk_normalizes_first_timestamp():
    """timestamp_us가 5초 오프셋에서 시작해도 첫 프레임이 t=0으로 정규화된다(미디어 상대초)."""
    rec = RecordingWindower()
    asyncio.run(ingest.consume_video_lk(FakeStream(video_events(64, fps=30, t0_us=5_000_000)), rec))
    times = [t for (t, _) in rec.frames]
    assert times[0] == 0.0, times[0]
    assert [round(t, 3) for t in times] == [0.0, 1.0, 2.0], times
    print(f"  ✓ t0=5s 오프셋 → 정규화 t={times}")


def test_consume_video_lk_closes_stream_on_eos():
    """EOS(async for 자연 종료) 시 stream.aclose()를 호출한다(정리)."""
    s = FakeStream(video_events(4, fps=30))
    asyncio.run(ingest.consume_video_lk(s, RecordingWindower()))
    assert s.closed, "EOS 후 aclose 호출돼야 함"
    print("  ✓ video EOS → aclose 호출")


# ── consume_audio_lk ──
def test_consume_audio_lk_accumulates_duration_and_passes_pcm():
    """오디오: 누적 frame.duration 미디어시계(첫 청크 t=0) + PCM bytes 무변경 passthrough(리샘플 없음)."""
    k = 10
    rec = RecordingWindower()
    asyncio.run(ingest.consume_audio_lk(FakeStream([audio_event(i) for i in range(k)]), rec))
    times = [t for (t, _) in rec.audio]
    assert len(times) == k
    # 누적 duration: 0, 0.02, 0.04, … (320/16000=0.02)
    for i, t in enumerate(times):
        assert abs(t - i * 0.02) < 1e-9, (i, t)
    # PCM passthrough: 각 청크는 입력 int16 bytes 그대로(16k mono → 변환 0).
    for i, (_, pcm) in enumerate(rec.audio):
        assert pcm == np.full(320, i % 1000, dtype=np.int16).tobytes(), i
        assert len(pcm) % 2 == 0
    total = sum(len(pcm) for (_, pcm) in rec.audio)
    assert total == k * 320 * 2  # 손실/변환 없음
    print(f"  ✓ {k}청크 누적 duration t0=0, PCM {total//2} 샘플 무변경 passthrough")


def test_consume_audio_lk_closes_stream_on_eos():
    s = FakeStream([audio_event(i) for i in range(3)])
    asyncio.run(ingest.consume_audio_lk(s, RecordingWindower()))
    assert s.closed, "EOS 후 aclose 호출돼야 함"
    print("  ✓ audio EOS → aclose 호출")


# ── 취소 전파 ──
def test_consumers_propagate_cancellation_and_close():
    """소비자는 CancelledError(태스크 취소)를 전파하고, finally에서 aclose는 호출한다.

    EOS(StopAsyncIteration)만 정상 종료로 흡수하고 취소는 위로 던져야 서버가 소비자 태스크를 멈출 수 있다."""
    for consume in (ingest.consume_video_lk, ingest.consume_audio_lk):
        s = CancellingStream()
        raised = False
        try:
            asyncio.run(consume(s, RecordingWindower()))
        except asyncio.CancelledError:
            raised = True
        assert raised, f"{consume.__name__}은 CancelledError를 전파해야 한다"
        assert s.closed, f"{consume.__name__}은 취소 시에도 aclose로 정리해야 한다"
    print("  ✓ 취소(CancelledError) 전파 + aclose 정리")


def main() -> int:
    tests = [
        test_encode_jpeg_rgb_tight_buffer,
        test_encode_jpeg_rgb_handles_row_stride,
        test_consume_video_lk_throttles_to_1fps,
        test_consume_video_lk_normalizes_first_timestamp,
        test_consume_video_lk_closes_stream_on_eos,
        test_consume_audio_lk_accumulates_duration_and_passes_pcm,
        test_consume_audio_lk_closes_stream_on_eos,
        test_consumers_propagate_cancellation_and_close,
    ]
    print(f"=== ingest_lk tests ({len(tests)}) ===")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
    print(f"=== {'ALL PASS' if not failed else f'{failed} FAILED'} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
