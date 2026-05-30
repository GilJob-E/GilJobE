#!/usr/bin/env python3
"""Slice 1 실데이터 검증 — 영상을 '스트리밍처럼' 윈도우로 쪼개 media/window_assembly 통과.

이건 정식 pytest가 아니라 .dev 스크래치 검증이다(gje 클립 경로에 결합되므로 tests/엔 안 둠).
미구현 컴포넌트를 수동 대체해 Slice 1 모듈만 실데이터로 돌린다:
  - ingest(S7 미구현)  → ffmpeg로 1fps JPEG + 16k mono PCM 추출로 대체
  - windowing(S4 미구현) → 3초 슬라이딩 윈도우 루프로 대체
  - 검증 대상         → Slice 1의 assemble_video_url / assemble_audio_url (실 프레임/오디오)

실행: PYTHONPATH=src .venv/bin/python .dev/verify_slice1_stream.py <clip.mp4> <out.json>
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from giljobe.media import window_assembly as wa

WINDOW_S = 3.0
SRC_FPS = 1.0
SR = 16000


def extract_frames_1fps(clip: str, d: str) -> list[bytes]:
    """ingest 대체: 1fps JPEG 프레임(시간순)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", clip, "-vf", f"fps={SRC_FPS:g}", "-q:v", "2", str(Path(d, "f%05d.jpg"))]
    subprocess.run(cmd, check=True, timeout=180)
    return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"))]


def extract_pcm_16k_mono(clip: str) -> bytes:
    """ingest 대체: 44.1k 스테레오 → 16k mono Int16 PCM (라이브 리샘플 경로와 동일 타깃)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", clip, "-vn", "-ar", str(SR), "-ac", "1", "-f", "s16le", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=180).stdout


def decode_data_url(url: str, mime: str) -> bytes:
    head, b64 = url.split(",", 1)
    assert head.startswith(f"data:{mime};base64"), head
    return base64.b64decode(b64)


def mp4_frame_count(mp4: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(mp4)
        f.flush()
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", f.name],
            capture_output=True, text=True, timeout=30,
        )
    return int(out.stdout.strip())


def main() -> int:
    clip = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else ".dev/slice1-stream-evidence.json"

    with tempfile.TemporaryDirectory() as d:
        frames = extract_frames_1fps(clip, d)
        pcm = extract_pcm_16k_mono(clip)

    res: dict = {
        "clip": clip, "window_s": WINDOW_S, "src_fps": SRC_FPS, "sample_rate": SR,
        "total_frames_1fps": len(frames), "total_audio_s": round(len(pcm) / 2 / SR, 2),
        "windows": [],
    }
    dur = max(len(frames) / SRC_FPS, len(pcm) / 2 / SR)

    t = 0.0
    all_ok = True
    while t < dur - 1e-9:
        fa, fb = int(round(t * SRC_FPS)), int(round((t + WINDOW_S) * SRC_FPS))
        win_frames = frames[fa:fb]
        ba, bb = int(t * SR) * 2, int((t + WINDOW_S) * SR) * 2
        win_pcm = pcm[ba:bb]
        rec: dict = {"t": round(t, 1), "n_frames_in": len(win_frames),
                     "pcm_ms": round(len(win_pcm) / 2 / SR * 1000)}

        if win_frames:
            try:
                mp4 = decode_data_url(wa.assemble_video_url(win_frames, src_fps=SRC_FPS), "video/mp4")
                nb = mp4_frame_count(mp4)
                rec.update(mp4_bytes=len(mp4), mp4_nb_frames=nb, video_ok=(nb == len(win_frames)))
            except Exception as e:  # noqa: BLE001
                rec.update(video_ok=False, video_err=str(e)[:140])
        else:
            rec["video_ok"] = None

        if win_pcm:
            try:
                wav = decode_data_url(wa.assemble_audio_url(win_pcm, sample_rate=SR), "audio/wav")
                with wave.open(io.BytesIO(wav), "rb") as w:
                    sr, ch, nf = w.getframerate(), w.getnchannels(), w.getnframes()
                rec.update(wav_sr=sr, wav_ch=ch, wav_frames=nf,
                           audio_ok=(sr == SR and ch == 1 and nf == len(win_pcm) // 2))
            except Exception as e:  # noqa: BLE001
                rec.update(audio_ok=False, audio_err=str(e)[:140])
        else:
            rec["audio_ok"] = None

        if rec.get("video_ok") is False or rec.get("audio_ok") is False:
            all_ok = False
        res["windows"].append(rec)
        t += WINDOW_S

    # 보너스: 전체 클립 한 번에 → 캡 동작(비디오 16프레임/오디오 30s) 실데이터 확인
    full_mp4 = decode_data_url(wa.assemble_video_url(frames, src_fps=SRC_FPS), "video/mp4")
    full_cap_frames = mp4_frame_count(full_mp4)
    full_wav = decode_data_url(wa.assemble_audio_url(pcm, sample_rate=SR), "audio/wav")
    with wave.open(io.BytesIO(full_wav), "rb") as w:
        full_cap_audio_s = round(w.getnframes() / SR, 2)
    res["fullclip_cap"] = {
        "video_frames_capped_to": full_cap_frames, "expected_video_cap": wa.VIDEO_MAX_SECONDS,
        "video_cap_ok": full_cap_frames == min(len(frames), wa.VIDEO_MAX_SECONDS),
        "audio_s_capped_to": full_cap_audio_s, "expected_audio_cap": wa.AUDIO_MAX_SECONDS,
        "audio_cap_ok": full_cap_audio_s == min(round(len(pcm) / 2 / SR, 2), float(wa.AUDIO_MAX_SECONDS)),
    }
    if not (res["fullclip_cap"]["video_cap_ok"] and res["fullclip_cap"]["audio_cap_ok"]):
        all_ok = False

    res["n_windows"] = len(res["windows"])
    res["ALL_OK"] = all_ok
    Path(out_path).write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print("ALL_OK" if all_ok else "FAILED", "| windows=", res["n_windows"], "| out=", out_path)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
