"""구독자 조립 — critic·sink·config로 windower+recorder+LiveKitSubscriber를 한 번에 묶는다.

이 팩토리가 통합 seam이다: 실서비스는 WindowCritic(실 vLLM)+JSONLSink(+ai-engine WebhookSink)로,
라이브 e2e는 MockCritic+JSONLSink로 같은 배선을 쓴다. room을 주입하면(테스트=fake) 그걸 쓰고,
없으면 실 rtc.Room()을 만든다.
"""
from __future__ import annotations

import os

from giljobe.emit.sink import SignalRecorder
from giljobe.pipeline.windowing import TurnWindower

from .config import SubscriberConfig
from .subscriber import LiveKitSubscriber


def _new_room():
    from livekit import rtc

    return rtc.Room()


def build_subscriber(
    critic,
    sinks,
    config: SubscriberConfig,
    *,
    room=None,
    executors=None,
    eot_deadline_s: float = 3.0,
    grounder=None,
    prosody=None,
    transcript_source: str | None = None,
    eval_grid: str | None = None,
):
    """windower(critic 주입)+recorder(sinks fan-out)+subscriber를 조립해 반환.

    executors=(nv, stt, eval, tail) 주입 시 그 4레인 스레드풀을 공유(앱-레벨), 미주입 시 윈도워가
    자체 생성. room 미주입 시 실 rtc.Room()(실 접속용).
    grounder/prosody: 객관 그라운딩 레인(선택, inject AND emit). ★grounder는 세션마다 새로
    만들어 넘길 것 — 윈도워가 수명(reset/close)을 소유한다(재사용 금지).
    transcript_source: internal(기본, gemma 3s 그리드) | external(PR #13 Realtime sideband —
    문장 레인이 nv·stt 그리드를 대체). None이면 env GILJOBE_TRANSCRIPT_SOURCE.
    eval_grid: wall(기본, 16s 벽시계) | sentence(문장 경계 정렬 누적 트리거 — external 전용) |
    off(발화중·tail 평가 모두 끔 — 내용 판단은 소비자 LLM 위임). None이면 env GILJOBE_EVAL_GRID."""
    if transcript_source is None:
        transcript_source = os.environ.get("GILJOBE_TRANSCRIPT_SOURCE", "internal").strip().lower()
    if eval_grid is None:
        eval_grid = os.environ.get("GILJOBE_EVAL_GRID", "wall").strip().lower()
    # JPEG 버퍼 fps(기본 1.0 — GilJob 규격). 올릴 땐 GILJOBE_NV_FRAME_CAP도 함께(캡이 무효화 방지).
    video_fps = float(os.environ.get("GILJOBE_FRAME_FPS", "0") or 0) or None
    recorder = SignalRecorder(config.session_id, sinks)
    if executors is None:
        windower = TurnWindower(
            critic, eot_deadline_s=eot_deadline_s, grounder=grounder, prosody=prosody,
            transcript_source=transcript_source, eval_grid=eval_grid,
            src_fps=video_fps or 1.0,
        )
    else:
        nv_x, stt_x, eval_x, tail_x = executors
        windower = TurnWindower(
            critic,
            nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
            eot_deadline_s=eot_deadline_s, grounder=grounder, prosody=prosody,
            transcript_source=transcript_source, eval_grid=eval_grid,
            src_fps=video_fps or 1.0,
        )
    return LiveKitSubscriber(
        room if room is not None else _new_room(),
        windower,
        recorder,
        poll_interval=config.poll_interval,
        video_fps=video_fps,
    )
