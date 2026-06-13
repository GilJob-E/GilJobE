"""ground-truth: .vision_test 손가락 시퀀스를 giljobe 비전 그라운더에 직접 통과시켜
해상도별 hand_seen_ratio·finger_sequence를 잰다. 제품경로(640x480 레터박스 fake cam)에서
시퀀스가 [4]로 붕괴한 게 (a)클립/알고리즘, (b)해상도, (c)라이브 전달 중 어디 문제인지 격리한다.

라이브 경로와 동일: GILJOBE_FRAME_FPS=3로 디코드 → VisionGrounder.add_frame(풀 fps 피드와 동형) →
window_metrics → aggregate_hands → _finger_segments. 라이브와 차이는 '전달'뿐(여긴 디스크 디코드).

실행: /home/kio/workspace/giljobe/.venv/bin/python .dev/grounding/vt_groundtruth.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "/home/kio/workspace/giljobe/src")
os.environ.setdefault("GILJOBE_VISION", "auto")

from giljobe.analysis.grounding import VisionGrounder  # noqa: E402

MODELS = Path("/home/kio/workspace/giljobe/.dev/vision/models")
CLIP = "/home/kio/workspace/giljob-docker/qa/.vision_test.mp4"
FPS = 3.0


def letterbox(img, W, H):
    h, w = img.shape[:2]
    s = min(W / w, H / h)
    nw, nh = int(round(w * s)), int(round(h * s))
    r = cv2.resize(img, (nw, nh))
    out = np.zeros((H, W, 3), np.uint8)
    y, x = (H - nh) // 2, (W - nw) // 2
    out[y:y + nh, x:x + nw] = r
    return out


def decode(clip, fps):
    cap = cv2.VideoCapture(clip)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(src_fps / fps))
    frames, i = [], 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            frames.append((i / src_fps, cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
        i += 1
    cap.release()
    return frames, src_fps


def run_variant(frames, transform):
    # 오프라인 전수 측정이라 load-shedding 비활성(라이브는 페이싱 도착이라 shed 0이었음).
    g = VisionGrounder(MODELS / "face_landmarker.task", MODELS / "pose_landmarker.task",
                       hand_model=MODELS / "hand_landmarker.task", max_backlog=10**9)
    g.reset()
    for t, rgb in frames:
        img = transform(rgb) if transform else rgb
        g.add_frame(t, np.ascontiguousarray(img))
    g.wait_idle(timeout=180)
    dur = frames[-1][0] + 1
    m = g.window_metrics(0.0, dur) or {}
    # 3s 윈도우별 손 검출 타임라인(native에서 시퀀스가 어디 흩어져 있는지)
    timeline = []
    t0 = 0.0
    while t0 < dur:
        wm = g.window_metrics(t0, t0 + 3.0) or {}
        timeline.append({"win": [round(t0, 1), round(t0 + 3, 1)],
                         "hand_seen": wm.get("hand_seen_ratio"),
                         "fingers": wm.get("finger_sequence")})
        t0 += 3.0
    g.close()
    return m, timeline


def main():
    frames, src_fps = decode(CLIP, FPS)
    nat = frames[0][1].shape
    print(f"decoded {len(frames)} frames @ {FPS}fps (src {src_fps:.1f}fps); native {nat[1]}x{nat[0]}")
    variants = {
        "native": None,
        "960x540": lambda im: cv2.resize(im, (960, 540)),
        "640x480-letterbox(harness)": lambda im: letterbox(im, 640, 480),
    }
    out = {}
    for label, tf in variants.items():
        m, timeline = run_variant(frames, tf)
        keys = ("hand_frames", "hand_seen_ratio", "finger_sequence", "finger_segments",
                "face_seen_ratio", "wrist_visible", "shed_frames_total")
        out[label] = {"summary": {k: m.get(k) for k in keys}, "timeline_3s": timeline}
        print(f"\n=== {label} ===")
        print("  ", json.dumps(out[label]["summary"], ensure_ascii=False))
        for row in timeline:
            print("   ", row)
    od = Path("/home/kio/workspace/giljob-docker/qa/runs/2026-06-13-vision-test-groundtruth")
    od.mkdir(parents=True, exist_ok=True)
    (od / "groundtruth.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(f"\n→ {od}/groundtruth.json")


if __name__ == "__main__":
    main()
