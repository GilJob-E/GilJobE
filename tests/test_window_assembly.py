#!/usr/bin/env python3
"""window_assembly 단위 테스트 — 합성 JPEG/PCM으로 조립·캡 검증 (GPU 불필요).

실행: pytest tests/test_window_assembly.py
      (standalone: PYTHONPATH=src python3 tests/test_window_assembly.py)
"""
from __future__ import annotations

import base64
import io
import subprocess
import tempfile
import wave

from giljobe.media import window_assembly as wa


def _make_jpeg(color_hex: str, w: int = 64, h: int = 48) -> bytes:
    """ffmpeg lavfi로 단색 JPEG 한 장 생성."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x{color_hex}:s={w}x{h}",
        "-frames:v", "1", "-f", "mjpeg", "pipe:1",
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=15)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:200]
    return out.stdout


def _mp4_frame_count(mp4: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(mp4)
        f.flush()
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames",
             "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
             "-of", "csv=p=0", f.name],
            capture_output=True, text=True, timeout=15,
        )
    return int(out.stdout.strip())


def _decode_data_url(url: str, expect_mime: str) -> bytes:
    head, b64 = url.split(",", 1)
    assert head.startswith(f"data:{expect_mime};base64"), head
    return base64.b64decode(b64)


def test_video_url_basic():
    frames = [_make_jpeg(h) for h in ("ff0000", "00ff00", "0000ff")]
    url = wa.assemble_video_url(frames, src_fps=1.0)
    mp4 = _decode_data_url(url, "video/mp4")
    n = _mp4_frame_count(mp4)
    assert n == 3, f"expected 3 frames, got {n}"
    print(f"  ✓ video_url basic: 3 frames → mp4 {len(mp4)}B, nb_frames={n}")


def test_video_url_caps_to_16():
    # 20프레임 @1fps → 16프레임으로 캡(최근 것)
    frames = [_make_jpeg(f"{i*5:02x}0000") for i in range(20)]
    url = wa.assemble_video_url(frames, src_fps=1.0)
    n = _mp4_frame_count(_decode_data_url(url, "video/mp4"))
    assert n == wa.VIDEO_MAX_SECONDS, f"expected cap {wa.VIDEO_MAX_SECONDS}, got {n}"
    print(f"  ✓ video_url cap: 20 frames → {n} (=VIDEO_MAX_SECONDS)")


def test_audio_url_basic():
    sr = 16000
    pcm = b"\x00\x00" * (sr * 5)  # 5초 무음 Int16
    url = wa.assemble_audio_url(pcm, sample_rate=sr)
    wav = _decode_data_url(url, "audio/wav")
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getframerate() == sr
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == sr * 5
    print(f"  ✓ audio_url basic: 5s 16kHz mono → wav {len(wav)}B, {w.getnframes()} frames")


def test_audio_url_caps_to_30s():
    sr = 16000
    pcm = b"\x00\x00" * (sr * 35)  # 35초 → 30초로 캡
    url = wa.assemble_audio_url(pcm, sample_rate=sr)
    with wave.open(io.BytesIO(_decode_data_url(url, "audio/wav")), "rb") as w:
        assert w.getnframes() == sr * wa.AUDIO_MAX_SECONDS, w.getnframes()
    print(f"  ✓ audio_url cap: 35s → {wa.AUDIO_MAX_SECONDS}s ({sr*wa.AUDIO_MAX_SECONDS} frames)")


def test_empty_raises():
    for fn, arg in ((wa.assemble_video_url, []), (wa.assemble_audio_url, b"")):
        try:
            fn(arg)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} should raise on empty")
    print("  ✓ empty input raises ValueError")


def main() -> int:
    tests = [
        test_video_url_basic, test_video_url_caps_to_16,
        test_audio_url_basic, test_audio_url_caps_to_30s, test_empty_raises,
    ]
    print(f"=== window_assembly tests ({len(tests)}) ===")
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
