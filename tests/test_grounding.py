"""test_grounding — 비전 dense 레인 오프라인 검증.

집계는 순수 함수라 mediapipe 없이 합성 행으로 직격하고(깜빡임 카운트·관측불가 게이팅·빈 윈도우),
레인 스레딩/수명(epoch reset·close·load-shedding)은 실 VisionGrounder로 검증한다(mediapipe +
모델 .task 게이트 — 없으면 skip). 실 얼굴 검출 수치의 정본 검증은 .dev/vision PoC evidence.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from giljobe.analysis.grounding import (
    aggregate_face,
    aggregate_pose,
    describe_visual,
    maybe_vision_grounder,
)

MODELS = Path(__file__).parent.parent / ".dev" / "vision" / "models"


def _face_row(t, *, smile=0.0, blink=0.0, yaw=0.0, pitch=0.0, gaze=0.0, lips=None):
    return {
        "t": t, "face": True, "smile": smile, "brow_down": 0.0, "press": 0.0,
        "blink": blink, "gaze_off": gaze, "yaw": yaw, "pitch": pitch,
        "lips": lips or [(0.4, 0.5), (0.6, 0.5), (0.5, 0.45), (0.5, 0.55)],
    }


def _pose_row(t, *, wr_l=(0.3, 0.8, 0.9), wr_r=(0.7, 0.8, 0.9), sh=(0.5, 0.5)):
    return {
        "t": t, "pose": True, "wrist_vis": max(wr_l[2], wr_r[2]),
        "sh_mid": sh, "sh_w": 0.2, "nose": (0.5, 0.3), "wr": {"l": wr_l, "r": wr_r},
    }


# ─────────────────────────── 집계 (순수 함수, mediapipe 불요) ───────────────────────────

def test_aggregate_face_empty_and_undetected():
    assert aggregate_face([]) == {"face_frames": 0}
    rows = [{"t": 0.1, "face": False}, {"t": 0.2, "face": False}]
    agg = aggregate_face(rows)
    assert agg == {"face_frames": 2, "face_seen_ratio": 0.0}  # 검출 0 → 수치 없음(단정 차단 신호)


def test_aggregate_face_smile_and_blinks():
    """깜빡임=임계(0.5) 상향 교차 횟수 — 1fps로는 못 세는 시계열 신호가 정확히 집계되는가."""
    blinks = [0.1, 0.7, 0.2, 0.9, 0.4, 0.6]  # 상향 교차 3회
    rows = [_face_row(i * 0.033, smile=s, blink=b)
            for i, (s, b) in enumerate(zip([0.0, 0.3, 0.6, 0.1, 0.5, 0.0], blinks))]
    agg = aggregate_face(rows)
    assert agg["blink_count"] == 3
    assert agg["smile_max"] == 0.6
    assert agg["smile_ratio"] == 0.5  # 6프레임 중 3개 >0.2
    assert agg["lip_motion"] == 0.0   # 입술 정지 → 모션 0("떨림" 상투구 반증 방향)


def test_aggregate_face_lip_motion_detects_movement():
    rows = [_face_row(0.0), _face_row(0.033, lips=[(0.41, 0.5), (0.61, 0.5), (0.5, 0.46), (0.5, 0.56)])]
    assert aggregate_face(rows)["lip_motion"] > 0.0


def test_aggregate_pose_wrist_invisible_vs_gesture():
    """손목 비가시(타이트 샷) → wrist_visible 0(관측 불가). 가시+큰 변위 → gesture_events 검출."""
    hidden = [_pose_row(i * 0.033, wr_l=(0.3, 0.9, 0.1), wr_r=(0.7, 0.9, 0.1)) for i in range(5)]
    agg = aggregate_pose(hidden)
    assert agg["wrist_visible"] == 0.0 and agg["gesture_events"] == 0
    moving = [_pose_row(0.0, wr_l=(0.30, 0.8, 0.9)), _pose_row(0.033, wr_l=(0.38, 0.8, 0.9))]
    agg2 = aggregate_pose(moving)  # 변위 0.08/어깨폭 0.2 = 0.4 > 0.15
    assert agg2["gesture_events"] >= 1 and agg2["wrist_motion"] > 0.15


def test_aggregate_pose_empty():
    assert aggregate_pose([]) == {"pose_frames": 0}


# ─────────────────────────── describe(주입 라벨 — 차단 게이팅) ───────────────────────────

def test_describe_visual_blocks_unobservable_gesture():
    m = {**aggregate_face([_face_row(0.0, smile=0.5)] * 3),
         **aggregate_pose([_pose_row(i * 0.1, wr_l=(0.3, 0.9, 0.1), wr_r=(0.7, 0.9, 0.1)) for i in range(3)])}
    text = describe_visual(m)
    assert "관측 불가" in text and "단정 금지" in text  # PoC 충돌 #3("제스처 못함") 차단
    assert "미소" in text
    assert "긴장" not in text  # 해석 라벨 금지


def test_describe_visual_no_face_blocks_expression():
    text = describe_visual({"face_frames": 5, "face_seen_ratio": 0.0})
    assert "얼굴 미검출" in text and "단정 금지" in text


def test_describe_visual_empty():
    assert describe_visual({}) == ""


# ─────────────────────────── 실 레인 (mediapipe + 모델 게이트) ───────────────────────────

def _make_grounder():
    pytest.importorskip("mediapipe", reason="vision extra 미설치([vision])")
    if not (MODELS / "face_landmarker.task").is_file():
        pytest.skip("MediaPipe 모델 없음(.dev/vision/models — README 재현법)")
    g = maybe_vision_grounder(MODELS)
    assert g is not None
    return g


def test_grounder_lane_coverage_reset_close():
    """합성(무얼굴) 프레임 → 커버리지 행 적재 → reset이 epoch로 격리 → close 후 입력 무시."""
    g = _make_grounder()
    try:
        blank = np.zeros((64, 64, 3), dtype=np.uint8)
        for i in range(4):
            g.add_frame(i * 0.1, blank)
        g.wait_idle(timeout=10.0)
        m = g.window_metrics(0.0, 1.0)
        assert m is not None and m["face_frames"] == 4 and m["face_seen_ratio"] == 0.0
        assert m["pose_frames"] == 4
        assert g.window_metrics(5.0, 8.0) is None  # 빈 윈도우 → None(주입/emit 생략 신호)

        g.reset()  # 새 턴 — 이전 행 격리(t가 0으로 되돌아가도 MediaPipe ts는 단조 유지)
        assert g.window_metrics(0.0, 1.0) is None
        g.add_frame(0.05, blank)
        g.wait_idle(timeout=10.0)
        assert g.window_metrics(0.0, 1.0)["face_frames"] == 1
    finally:
        g.close()
    g.add_frame(0.2, np.zeros((8, 8, 3), dtype=np.uint8))  # close 후 no-op(예외 없음)


def test_maybe_factory_disabled_paths(monkeypatch):
    monkeypatch.setenv("GILJOBE_VISION", "off")
    assert maybe_vision_grounder(MODELS) is None
    monkeypatch.setenv("GILJOBE_VISION", "auto")
    monkeypatch.delenv("GILJOBE_VISION_MODELS_DIR", raising=False)
    assert maybe_vision_grounder() is None  # 모델 경로 미지정 → 비활성
    assert maybe_vision_grounder("/nonexistent") is None  # 모델 없음 → 비활성
