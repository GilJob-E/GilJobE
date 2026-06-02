#!/usr/bin/env python3
"""ingest 단위 테스트 — 합성 PyAV 프레임으로 변환·throttle·resample 검증 (GPU/브라우저 불요).

핵심 커버(PLAN §6·§리스크):
  - ★ resample-list gotcha: AudioResampler.resample()은 list 반환(작은 입력=빈 list, 꼬리는 flush로만)
    → 순회+concat+flush 안 하면 손상·누락. 손실을 직접 assert.
  - 1fps throttle: ~30fps 입력 → t≈0,1,2…만 통과.
  - PCM Int16 정렬: 청크 길이 짝수(2바이트/샘플), 48k→16k mono 3:1 보존.
  - JPEG 640×480 인코드, 트랙 종료(MediaStreamError) 시 소비자 반환 + flush 꼬리 배출.

실행: pytest tests/test_ingest_slicing.py
      (standalone: PYTHONPATH=src python3 tests/test_ingest_slicing.py)
"""
from __future__ import annotations

import asyncio
import io
from fractions import Fraction

import av
import numpy as np
from PIL import Image

from aiortc.mediastreams import MediaStreamError
from giljobe.media import ingest

SR_IN = 48000          # 브라우저 Opus 디코드 레이트
SAMPLES_PER_FRAME = 960  # 20ms @48k
RATIO = SR_IN // ingest.SAMPLE_RATE  # 48k→16k = 3:1 (정수 → 총 샘플 보존이 정확)
OUT_PER_FRAME = SAMPLES_PER_FRAME // RATIO  # 프레임당 16k 출력 샘플(=320)


# ── 합성 프레임 빌더 ──
def make_video_frame(idx: int, *, w: int = 1280, h: int = 720, fps: int = 30, with_pts: bool = True):
    arr = np.full((h, w, 3), (idx * 7) % 255, dtype=np.uint8)
    f = av.VideoFrame.from_ndarray(arr, format="rgb24")
    if with_pts:
        f.pts = idx
        f.time_base = Fraction(1, fps)
    return f


def make_audio_frame(idx: int, *, samples: int = SAMPLES_PER_FRAME, rate: int = SR_IN,
                     layout: str = "stereo", with_pts: bool = True):
    ch = 2 if layout == "stereo" else 1
    arr = (np.arange(samples * ch, dtype=np.int16) % 1000).reshape(1, samples * ch)
    f = av.AudioFrame.from_ndarray(arr, format="s16", layout=layout)
    f.sample_rate = rate
    if with_pts:
        f.pts = idx * samples
        f.time_base = Fraction(1, rate)
    return f


class FakeTrack:
    """프레임 리스트를 순서대로 내고 소진되면 MediaStreamError(=실 aiortc 트랙 종료 신호)."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    async def recv(self):
        if self._i >= len(self._frames):
            raise MediaStreamError
        f = self._frames[self._i]
        self._i += 1
        return f


class RecordingWindower:
    """_Windower 표면을 만족하는 더블 — add_frame/add_audio 호출을 (t, payload)로 기록."""

    def __init__(self):
        self.frames: list[tuple[float, bytes]] = []
        self.audio: list[tuple[float, bytes]] = []

    def add_frame(self, t_s: float, jpeg: bytes) -> None:
        self.frames.append((t_s, jpeg))

    def add_audio(self, t_s: float, pcm: bytes) -> None:
        self.audio.append((t_s, pcm))


# ── resample-list gotcha (★ 핵심) ──
def test_resample_returns_list_and_small_frame_is_empty():
    """resample()은 list를 반환하고, 너무 작은 입력은 빈 list다 → result[0]는 IndexError(gotcha)."""
    r = av.AudioResampler(format="s16", layout="mono", rate=ingest.SAMPLE_RATE)
    out = r.resample(make_audio_frame(0, samples=3))  # 3 samples @48k → <1 출력
    assert isinstance(out, list), "resample()은 list를 반환한다(단일 프레임 아님)"
    assert out == [], "작은 입력은 빈 list → [0] 접근 시 IndexError(미순회 코드가 깨지는 지점)"
    # 그 뒤 정상 프레임에서 출력이 나옴(버퍼 누적) → '프레임당 1개'를 가정하면 안 됨을 보강
    out2 = r.resample(make_audio_frame(1))
    assert isinstance(out2, list) and len(out2) >= 1
    print("  ✓ resample()=list, 작은 입력=빈 list(미순회 시 IndexError/손상)")


def test_pcm_resampler_preserves_all_samples_via_iterate_and_flush():
    """전 프레임 순회+concat+flush → 48k→16k mono로 총 샘플이 정확히 보존(K*320), 모두 Int16 정렬."""
    k = 8
    pr = ingest.PcmResampler()
    out = b""
    for i in range(k):
        chunk = pr.resample(make_audio_frame(i))
        assert len(chunk) % 2 == 0, "각 청크는 Int16 정렬(2바이트/샘플)"
        out += chunk
    out += pr.flush()
    expected_samples = k * OUT_PER_FRAME  # 3:1 정수비라 총량 정확 보존
    assert len(out) == expected_samples * 2, (len(out), expected_samples * 2)
    print(f"  ✓ resample+flush 총 샘플 보존: {k}프레임 → {len(out)//2} 샘플(=K*{OUT_PER_FRAME})")


def test_skipping_flush_loses_tail():
    """flush를 빼면 필터 지연 꼬리 샘플이 누락된다 → flush가 load-bearing임을 직접 증명."""
    k = 8
    pr = ingest.PcmResampler()
    without_flush = b"".join(pr.resample(make_audio_frame(i)) for i in range(k))
    tail = pr.flush()
    assert len(tail) > 0, "flush는 버퍼에 남은 꼬리 샘플을 배출해야 한다"
    with_flush = without_flush + tail
    assert len(without_flush) < len(with_flush), "flush 생략 시 끝부분 PCM 누락(손상)"
    assert len(with_flush) == k * OUT_PER_FRAME * 2
    print(f"  ✓ flush 생략 시 꼬리 {len(tail)//2} 샘플 누락 → flush 필수")


def test_resample_is_16k_mono():
    """입력 48k 스테레오 → 출력이 16k mono(3:1)임을 출력 샘플 수로 검증."""
    pr = ingest.PcmResampler()
    chunk = pr.resample(make_audio_frame(0)) + pr.flush()
    # mono Int16: bytes/2 = 샘플 수. 48k 960 stereo → 16k 320 mono.
    assert len(chunk) // 2 == OUT_PER_FRAME, len(chunk) // 2
    print(f"  ✓ 48k stereo {SAMPLES_PER_FRAME} → 16k mono {len(chunk)//2} 샘플(3:1)")


def test_resample_handles_mono_input():
    """브라우저가 mono로 publish해도(스테레오 가정 깨짐) 16k mono PCM이 정확히 보존된다."""
    pr = ingest.PcmResampler()
    out = pr.resample(make_audio_frame(0, layout="mono")) + pr.flush()
    assert len(out) // 2 == OUT_PER_FRAME, len(out) // 2
    print(f"  ✓ mono 48k 입력 → 16k mono {len(out)//2} 샘플(레이아웃 가정 비의존)")


# ── JPEG 인코드 ──
def test_encode_jpeg_from_yuv420p():
    """실제 웹캠/디코드 포맷은 rgb24가 아니라 yuv420p — reformat이 흡수해 640×480 JPEG가 나온다."""
    yuv = av.VideoFrame(1280, 720, "yuv420p")  # 디코더가 주는 평면 포맷
    yuv.pts = 0
    yuv.time_base = Fraction(1, 30)
    jpeg = ingest.encode_jpeg(yuv)
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (ingest.VIDEO_WIDTH, ingest.VIDEO_HEIGHT) and img.format == "JPEG"
    print(f"  ✓ yuv420p 1280×720 → JPEG {len(jpeg)}B, size={img.size}")


# ── JPEG 인코드 ──
def test_encode_jpeg_640x480():
    """임의 해상도 VideoFrame → 640×480 JPEG bytes로 인코드되고 디코드 라운드트립 성립."""
    jpeg = ingest.encode_jpeg(make_video_frame(0, w=1280, h=720))
    assert jpeg[:3] == b"\xff\xd8\xff", "JPEG SOI 매직"
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (ingest.VIDEO_WIDTH, ingest.VIDEO_HEIGHT), img.size
    assert img.format == "JPEG"
    print(f"  ✓ 1280×720 → JPEG {len(jpeg)}B, decode size={img.size}")


# ── 트랙 소비자 (throttle / flush / 종료) ──
def test_consume_video_throttles_to_1fps():
    """30fps 입력 2.1초 → 1fps throttle로 t≈0,1,2 세 프레임만 윈도워에 투입(나머지 드롭)."""
    frames = [make_video_frame(i, fps=30) for i in range(64)]  # 0..2.1s
    rec = RecordingWindower()
    asyncio.run(ingest.consume_video(FakeTrack(frames), rec))
    times = [t for (t, _) in rec.frames]
    assert len(times) == 3, times
    for got, want in zip(times, (0.0, 1.0, 2.0)):
        assert abs(got - want) < 1e-6, (got, want)
    # 투입된 건 전부 640×480 JPEG
    for _, jpeg in rec.frames:
        assert Image.open(io.BytesIO(jpeg)).size == (640, 480)
    print(f"  ✓ 64프레임@30fps → 3프레임@1fps t={times}")


def test_consume_video_fallback_on_missing_pts():
    """pts 없는 프레임도 fallback 간격(idx/fps)으로 deterministic하게 throttle된다(degenerate 트랙)."""
    frames = [make_video_frame(i, fps=30, with_pts=False) for i in range(64)]
    rec = RecordingWindower()
    # target_fps=1 → fallback 간격 1.0초 가정 → 모든 프레임이 1.0초 간격으로 보여 64개 통과
    asyncio.run(ingest.consume_video(FakeTrack(frames), rec))
    times = [t for (t, _) in rec.frames]
    assert len(times) == 64, len(times)  # fallback이 frame당 interval → 전부 due
    assert times[0] == 0.0 and times[1] == 1.0
    print(f"  ✓ pts 없음 → fallback 간격으로 {len(times)}프레임(t0={times[0]}, t1={times[1]})")


def test_consume_audio_feeds_pcm_and_flushes_tail():
    """오디오 트랙 → 청크별 add_audio + 종료 시 flush 꼬리. 총 PCM 보존·정렬·타임스탬프 단조증가."""
    k = 10
    frames = [make_audio_frame(i) for i in range(k)]
    rec = RecordingWindower()
    asyncio.run(ingest.consume_audio(FakeTrack(frames), rec))
    assert len(rec.audio) >= 1
    total = b"".join(pcm for (_, pcm) in rec.audio)
    assert all(len(pcm) % 2 == 0 for (_, pcm) in rec.audio), "모든 청크 Int16 정렬"
    assert len(total) == k * OUT_PER_FRAME * 2, (len(total), k * OUT_PER_FRAME * 2)  # flush 포함 보존
    times = [t for (t, _) in rec.audio]
    assert times[0] == 0.0, times[0]
    assert times == sorted(times), "타임스탬프 단조증가(스트림 순서)"
    print(f"  ✓ {k}프레임 → {len(rec.audio)}청크, 총 {len(total)//2} 샘플(flush 포함), t0={times[0]}")


def test_consumers_terminate_on_empty_track():
    """빈 트랙(즉시 MediaStreamError) → 두 소비자 모두 즉시 반환, 투입 0(루프 종료 경로)."""
    rec = RecordingWindower()
    asyncio.run(ingest.consume_video(FakeTrack([]), rec))
    asyncio.run(ingest.consume_audio(FakeTrack([]), rec))
    assert rec.frames == [] and rec.audio == []
    print("  ✓ 빈 트랙 → 소비자 즉시 종료, 투입 0")


def test_consumers_propagate_cancellation():
    """소비자는 MediaStreamError만 잡고 CancelledError(태스크 취소=턴 종료/연결 해제)는 전파해야 한다.

    (취소를 삼키면 서버가 소비자 태스크를 못 멈춘다. MediaStreamError ⊂ Exception, CancelledError ⊂
    BaseException이라 except MediaStreamError는 취소를 잡지 않음을 동작으로 잠근다.)
    """
    class CancellingTrack:
        async def recv(self):
            raise asyncio.CancelledError

    for consume in (ingest.consume_video, ingest.consume_audio):
        raised = False
        try:
            asyncio.run(consume(CancellingTrack(), RecordingWindower()))
        except asyncio.CancelledError:
            raised = True
        assert raised, f"{consume.__name__}은 CancelledError를 전파해야 한다(삼키면 안 됨)"
    print("  ✓ 취소(CancelledError) 전파 — MediaStreamError만 흡수")


def main() -> int:
    tests = [
        test_resample_returns_list_and_small_frame_is_empty,
        test_pcm_resampler_preserves_all_samples_via_iterate_and_flush,
        test_skipping_flush_loses_tail,
        test_resample_is_16k_mono,
        test_resample_handles_mono_input,
        test_encode_jpeg_640x480,
        test_encode_jpeg_from_yuv420p,
        test_consume_video_throttles_to_1fps,
        test_consume_video_fallback_on_missing_pts,
        test_consume_audio_feeds_pcm_and_flushes_tail,
        test_consumers_terminate_on_empty_track,
        test_consumers_propagate_cancellation,
    ]
    print(f"=== ingest tests ({len(tests)}) ===")
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
