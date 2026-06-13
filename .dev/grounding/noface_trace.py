"""트레이스: 캠에 얼굴이 없을 때 소비자 fragment가 '얼굴 보인다'로 오독되는 경로 재현.

얼굴 없는 프레임(벽/빈 화면 모사) → VisionGrounder → aggregate → turnHandoff.visual →
(a) render_prompt_fragment(소비자가 받는 것)  vs  (b) describe_visual(분석 측 verbose).
비대칭(소비자 가드가 face_seen_ratio를 안 봄)을 출력으로 드러낸다.

실행: /home/kio/workspace/giljobe/.venv/bin/python .dev/grounding/noface_trace.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/kio/workspace/giljobe/src")

from giljobe.analysis.grounding import VisionGrounder, describe_visual  # noqa: E402
from giljobe.emit.handoff import agg_visual, render_prompt_fragment  # noqa: E402

MODELS = Path("/home/kio/workspace/giljobe/.dev/vision/models")


def main() -> None:
    g = VisionGrounder(MODELS / "face_landmarker.task", MODELS / "pose_landmarker.task",
                       hand_model=MODELS / "hand_landmarker.task", max_backlog=10**9)
    g.reset()
    # 얼굴 없는 캠: 균일 회색 프레임(벽 모사) 30장 @3fps 구간
    blank = np.full((480, 640, 3), 128, dtype=np.uint8)
    for i in range(30):
        g.add_frame(i / 3.0, blank)
    g.wait_idle(timeout=60)
    win = g.window_metrics(0.0, 11.0) or {}
    g.close()

    print("=== per-window metrics (grounder 직접 출력) ===")
    print("  face_frames:", win.get("face_frames"), "| face_seen_ratio:", win.get("face_seen_ratio"))
    print("  smile_mean 키 존재?:", "smile_mean" in win, "| gaze_off_mean 키 존재?:", "gaze_off_mean" in win)

    vis = agg_visual([win])
    print("\n=== turnHandoff.visual (agg_visual) ===")
    print("  face_frames:", vis.get("face_frames"), "| face_seen_ratio:", vis.get("face_seen_ratio"))
    print("  smile_mean:", vis.get("smile_mean"), "| gaze_off_mean:", vis.get("gaze_off_mean"))

    handoff = {"speech": {}, "visual": vis, "nonverbal": {}, "sentences": []}

    print("\n=== (a) 소비자가 받는 fragment — render_prompt_fragment ===")
    frag = render_prompt_fragment(handoff)
    print("  ", repr(frag))
    print("  → '시각 실측' 라인 포함?:", "시각 실측" in frag)

    print("\n=== (b) 분석 측 verbose — describe_visual ===")
    print("  ", repr(describe_visual(vis)))


if __name__ == "__main__":
    main()
