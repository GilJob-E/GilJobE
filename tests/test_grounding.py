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
    _FINGERS,
    aggregate_face,
    aggregate_hands,
    aggregate_pose,
    count_extended_fingers,
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


# ─────────────────────────── 손가락 카운트 (순수 함수, mediapipe 불요) ───────────────────────────

def _hand_pts(fingers=(True, True, True, True), thumb=True):
    """합성 21랜드마크 — 손목(0,0,0)·중지MCP(0,1,0) 기준. 반경비(펴짐 1.5/접힘 0.67)와
    엄지 거리(폄 1.0/접힘 0.2)만 기준을 만족시키는 최소 기하."""
    pts = [(0.0, 0.0, 0.0)] * 21
    pts[9] = (0.0, 1.0, 0.0)
    for ext, (pip, tip) in zip(fingers, _FINGERS):
        pts[pip] = (0.0, 1.2, 0.0)
        pts[tip] = (0.0, 1.8, 0.0) if ext else (0.0, 0.8, 0.0)
    pts[4] = (1.0, 1.0, 0.0) if thumb else (0.2, 1.0, 0.0)
    return pts


def test_count_extended_fingers_basic_shapes():
    assert count_extended_fingers(_hand_pts()) == 5                                          # 보자기
    assert count_extended_fingers(_hand_pts((False,) * 4, thumb=False)) == 0                 # 주먹
    assert count_extended_fingers(_hand_pts((True, True, False, False), thumb=False)) == 2   # V


def test_count_rejects_hallucinated_marginal_finger():
    """가림 환각 대역(반경비 ~1.1)은 펴짐으로 세지 않는다 — 기준 영상 '3'의 접힌 검지 케이스."""
    pts = _hand_pts((False, True, True, True), thumb=False)
    pts[8] = (0.0, 1.32, 0.0)  # 검지 끝 반경비 1.32/1.2=1.1 < FINGER_RADIAL
    assert count_extended_fingers(pts) == 3


def _hand_seq_rows(spec: list[tuple[int | None, int]]) -> list[dict]:
    """[(카운트|None=미검출, 행 수)] → 0.1s 간격 hand 행."""
    rows, t = [], 0.0
    for count, n in spec:
        for _ in range(n):
            rows.append({"t": round(t, 2), "hands": 0} if count is None
                        else {"t": round(t, 2), "hands": 1, "fingers": count})
            t = round(t + 0.1, 2)
    return rows


def test_aggregate_hands_empty_and_unseen():
    assert aggregate_hands([]) == {"hand_frames": 0}
    agg = aggregate_hands(_hand_seq_rows([(None, 2)]))
    assert agg == {"hand_frames": 2, "hand_seen_ratio": 0.0}  # 미검출 → 시퀀스 없음(단정 차단)


def test_aggregate_hands_stable_segments_and_sequence():
    """3(1s)→전이 1행→주먹(0.6s)→2(1s)→짧은 1(0.2s, 전이로 버려짐)→5(1s) = 시퀀스 3→2→5.
    주먹(0)은 segments에 사실로 남고 시퀀스에선 빠진다 — 기준 영상 3-2-5 패턴."""
    rows = _hand_seq_rows([(3, 10), (4, 1), (0, 6), (2, 10), (1, 2), (5, 10)])
    agg = aggregate_hands(rows)
    assert agg["hand_seen_ratio"] == 1.0
    assert agg["finger_sequence"] == [3, 2, 5]
    assert [s["count"] for s in agg["finger_segments"]] == [3, 0, 2, 5]


def test_describe_visual_finger_sequence_line():
    m = {"finger_sequence": [3, 2, 5], "hand_seen_ratio": 0.4,
         "finger_segments": [{"count": 3, "start_s": 1.0, "end_s": 2.0},
                             {"count": 0, "start_s": 2.2, "end_s": 3.0},
                             {"count": 2, "start_s": 3.1, "end_s": 4.0},
                             {"count": 5, "start_s": 4.4, "end_s": 5.2}]}
    text = describe_visual(m)
    assert "3→2→5" in text
    assert "0개" not in text  # 주먹 스팬은 라벨 라인에 안 끼운다


# ─────────────────────────── describe(주입 라벨 — 차단 게이팅) ───────────────────────────

def test_describe_visual_unobservable_gesture_no_guard():
    """손목 미가시 시 '관측 불가/평가 금지' 가드를 넣지 않는다(가드 제거 — 손가락 펼침 수치를
    상쇄하던 모순 라인 삭제). 손동작 라인은 손목 가시일 때만, 미소 등 나머지는 그대로."""
    m = {**aggregate_face([_face_row(0.0, smile=0.5)] * 3),
         **aggregate_pose([_pose_row(i * 0.1, wr_l=(0.3, 0.9, 0.1), wr_r=(0.7, 0.9, 0.1)) for i in range(3)])}
    text = describe_visual(m)
    assert "관측 불가" not in text and "단정 금지" not in text  # 손목 가드 제거
    assert "손동작" not in text                                  # 손목 미가시 → 손동작 라인도 없음
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
        assert m["hand_frames"] == 4 and m["hand_seen_ratio"] == 0.0  # 모델 있으면 손 레인 동행
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
