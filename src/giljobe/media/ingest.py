"""WebRTC 트랙 소비자 — 라이브 getUserMedia 스트림을 TurnWindower 입력 규격으로 변환·투입.

브라우저가 보내는 미디어 트랙(aiortc MediaStreamTrack)을 받아:
  - video(브라우저 기본 ~30fps) → ~1fps throttle → 640×480 JPEG bytes
  - audio(Opus 48kHz 스테레오)   → 16kHz mono Int16 PCM bytes
각 청크에 스트림 시작 기준 타임스탬프(초)를 붙여 윈도워의 add_frame/add_audio로 넣는다.
규격은 media/window_assembly.py(640×480 JPEG @1fps, 16kHz mono Int16)와 정확히 맞춘다.

poll(now)·turn 경계(start/end) 구동은 ingest의 일이 아니다 — 서버(Slice 8)가 타이머로 돌린다.
이유: poll은 프레임이 끊겨도(침묵 구간) 계속 떠 완료된 윈도우를 flush해야 하므로 프레임 도착이
아니라 서버 타이머에 매달려야 한다. ingest는 "트랙→윈도워 투입" 한 가지만 한다(단일 책임).

★ 이벤트루프 비차단: recv()만 await하고, 변환(reformat·JPEG·resample)은 ms급 CPU라 코루틴에서
  직접 한다(1fps 인코드·~20ms 리샘플 = 무시 가능). **추론(critic/STT, 3–7s 블로킹)은 절대 여기서
  호출하지 않는다** — 윈도워 executor 경유(PLAN §리스크 "이벤트루프 블로킹").
★ resample-list gotcha: av.AudioResampler.resample(frame)은 **list[AudioFrame]**를 반환한다 —
  작은 입력은 빈 list([0]=IndexError), 필터 지연으로 마지막 꼬리 샘플은 flush(resample(None))로만
  나온다. 전 프레임 순회+concat+flush를 안 하면 오디오가 손상·누락된다(PLAN §리스크, 명시 테스트).

레이어링: media는 리프 — TurnWindower를 import하지 않고 구조적 Protocol(_Windower)에만 의존한다
  (의존 방향 단방향: analysis→media, pipeline→analysis…; media→pipeline 역참조 금지).
"""
from __future__ import annotations

import io
import logging
from typing import Protocol

import av
from aiortc.mediastreams import MediaStreamError

logger = logging.getLogger(__name__)

VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
TARGET_FPS = 1.0       # GilJob 규격: 1fps JPEG(window_assembly와 일치)
SAMPLE_RATE = 16000    # GilJob 규격: 16kHz mono Int16 PCM
JPEG_QUALITY = 85
_AUDIO_FALLBACK_DUR = 0.02  # pts 없는 오디오 프레임 fallback 간격(20ms Opus 가정)


class _Windower(Protocol):
    """ingest가 의존하는 윈도워 표면(TurnWindower가 만족). media→pipeline 역참조를 피해 leaf 유지."""

    def add_frame(self, t_s: float, jpeg: bytes) -> None: ...
    def add_audio(self, t_s: float, pcm: bytes) -> None: ...


# ── 순수 변환 (트랙·이벤트루프 불요 — 단위 테스트 직격) ──
def encode_jpeg(
    frame: av.VideoFrame,
    *,
    size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT),
    quality: int = JPEG_QUALITY,
) -> bytes:
    """av.VideoFrame → 640×480 JPEG bytes. reformat(swscale)로 스케일, PIL로 인코드(cv2 회피)."""
    rf = frame.reformat(width=size[0], height=size[1], format="rgb24")
    buf = io.BytesIO()
    rf.to_image().save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class PcmResampler:
    """Opus 48kHz(스테레오 등) → 16kHz mono Int16 PCM. 리샘플러 1개를 스트림 내내 재사용한다
    — 프레임 경계 샘플을 내부 버퍼가 이어붙이므로 매 프레임 새로 만들면 경계마다 끊긴다."""

    def __init__(self, *, sample_rate: int = SAMPLE_RATE) -> None:
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)

    def resample(self, frame: av.AudioFrame) -> bytes:
        """프레임 1개 → PCM bytes. ★ resample()은 list 반환 — 전 프레임 순회·concat 필수."""
        return _concat_pcm(self._resampler.resample(frame))

    def flush(self) -> bytes:
        """스트림 끝 — 필터 지연으로 버퍼에 남은 꼬리 샘플 배출(안 하면 끝부분 누락)."""
        return _concat_pcm(self._resampler.resample(None))


def _concat_pcm(frames: list[av.AudioFrame]) -> bytes:
    # mono s16 → to_ndarray()=(1, samples) int16, tobytes()=정렬된 raw PCM. 빈 list면 b"".
    return b"".join(f.to_ndarray().tobytes() for f in frames)


def _frame_time(frame: av.VideoFrame | av.AudioFrame, fallback: float) -> float:
    """프레임 미디어 시각(초)=pts*time_base. aiortc 트랙은 항상 채우지만, 없으면 fallback."""
    t = frame.time
    return fallback if t is None else float(t)


# ── 트랙 소비자 (얇은 async glue) ──
async def consume_video(
    track,
    windower: _Windower,
    *,
    target_fps: float = TARGET_FPS,
    size: tuple[int, int] = (VIDEO_WIDTH, VIDEO_HEIGHT),
    quality: int = JPEG_QUALITY,
) -> None:
    """video 트랙을 ~target_fps로 throttle해 JPEG로 윈도워에 투입. 트랙 종료(MediaStreamError) 시 반환.

    타임스탬프는 첫 프레임 기준 상대초(t0=0). drop된 프레임은 인코드하지 않아 비용이 거의 없다.
    """
    interval = 1.0 / target_fps
    t0: float | None = None
    emit_at = 0.0
    idx = 0
    while True:
        try:
            frame = await track.recv()
        except MediaStreamError:
            break
        rel = _frame_time(frame, idx * interval)
        idx += 1
        if t0 is None:
            t0 = rel
        rel -= t0
        if rel + 1e-6 < emit_at:  # throttle: interval 안 지났으면 드롭(인코드 skip)
            continue
        emit_at = rel + interval
        windower.add_frame(rel, encode_jpeg(frame, size=size, quality=quality))


async def consume_audio(
    track,
    windower: _Windower,
    *,
    sample_rate: int = SAMPLE_RATE,
) -> None:
    """audio 트랙을 16k mono Int16 PCM으로 리샘플해 윈도워에 투입. 종료 시 flush로 꼬리까지 배출.

    각 청크(~20ms)에 시작 시각(첫 프레임 기준 상대초)을 붙인다 — 윈도워가 t로 3s 윈도우에 담는다.
    """
    resampler = PcmResampler(sample_rate=sample_rate)
    t0: float | None = None
    last_rel = 0.0
    idx = 0
    while True:
        try:
            frame = await track.recv()
        except MediaStreamError:
            break
        rel = _frame_time(frame, idx * _AUDIO_FALLBACK_DUR)
        idx += 1
        if t0 is None:
            t0 = rel
        last_rel = rel - t0
        pcm = resampler.resample(frame)
        if pcm:
            windower.add_audio(last_rel, pcm)
    tail = resampler.flush()  # 필터 지연 꼬리 — 안 배출하면 끝부분 누락
    if tail:
        windower.add_audio(last_rel, tail)
