"""윈도우 미디어 조립 — GilJob 탭 입력(1fps JPEG 프레임 + 16kHz PCM)을
E4B 전송용 `video_url`(mp4) / `audio_url`(wav) data URL로 만든다.

GilJob 규격(고정): 비디오 = 640×480 JPEG @1fps, 오디오 = 16kHz mono PCM Int16.
캡: 비디오 ≤16초(=16프레임 @1fps, Gemma ~32프레임 상한 이내·D8), 오디오 ≤30초.
multi-image 덤프는 시간축 binding이 깨지므로(D7) 쓰지 않고 항상 mp4 video_url로 보낸다.
"""
from __future__ import annotations

import base64
import io
import subprocess
import tempfile
import wave
from pathlib import Path

VIDEO_MAX_SECONDS = 16
AUDIO_MAX_SECONDS = 30
DEFAULT_SRC_FPS = 1.0
DEFAULT_SAMPLE_RATE = 16000


def assemble_video_url(jpeg_frames: list[bytes], *, src_fps: float = DEFAULT_SRC_FPS) -> str:
    """1fps JPEG 프레임들 → mp4 → `data:video/mp4;base64,...`.

    프레임 수가 캡(VIDEO_MAX_SECONDS * src_fps)을 넘으면 **최근** 것만 남긴다(슬라이딩).
    """
    if not jpeg_frames:
        raise ValueError("jpeg_frames is empty")
    max_frames = max(1, int(round(VIDEO_MAX_SECONDS * src_fps)))
    frames = jpeg_frames[-max_frames:]
    mp4 = _encode_mp4(frames, fps=src_fps)
    return "data:video/mp4;base64," + base64.b64encode(mp4).decode("ascii")


def assemble_audio_url(pcm: bytes, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> str:
    """16kHz mono PCM Int16 바이트 → WAV → `data:audio/wav;base64,...`.

    길이가 AUDIO_MAX_SECONDS를 넘으면 **최근** 구간만 남긴다(슬라이딩).
    """
    if not pcm:
        raise ValueError("pcm is empty")
    max_bytes = AUDIO_MAX_SECONDS * sample_rate * 2  # Int16 = 2 bytes/sample
    clipped = pcm[-max_bytes:]
    # Int16 정렬 보존(홀수 바이트 잘림 방지)
    if len(clipped) % 2:
        clipped = clipped[1:]
    wav = _wrap_wav(clipped, sample_rate=sample_rate)
    return "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")


def _encode_mp4(jpeg_frames: list[bytes], *, fps: float) -> bytes:
    """번호 프레임을 임시 디렉터리에 기록 → ffmpeg image2 시퀀스 → mp4 바이트.

    (concat-JPEG을 파이프로 먹이면 image2pipe 스트림 파싱이 깨져서 temp 파일 경로 사용.)
    """
    with tempfile.TemporaryDirectory() as d:
        for i, fr in enumerate(jpeg_frames):
            Path(d, f"f{i:05d}.jpg").write_bytes(fr)
        out = Path(d, "out.mp4")
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", f"{fps:g}", "-i", str(Path(d, "f%05d.jpg")),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg mp4 encode failed: {proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        return out.read_bytes()


def _wrap_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    """raw mono Int16 PCM → WAV 컨테이너(헤더만 추가, 무손실)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # Int16
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
