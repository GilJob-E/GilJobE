"""손가락 카운트 진단 프로브 — vision_test의 셈 구간을 프레임 단위로 본다.

목적: 3→2→5 정답 대비 미스카운트(특히 '3'→4/5)가 ① 랜드마크 품질(해상도)인지
② 카운트 휴리스틱(PIP 각/엄지 거리)인지 분리. 행마다 손별 네 손가락 PIP 각도와
엄지 margin(tip-새끼MCP 거리 - MCP-새끼MCP 거리; 양수=신전 판정)을 찍는다.

실행: .venv/bin/python .dev/grounding/hands_probe.py [width]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import math
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from giljobe.analysis.grounding import _FINGERS, _MIDDLE_MCP, _THUMB_TIP, count_extended_fingers

SRC = Path("/home/kio/workspace/giljob-docker/qa/.vision_test.mp4")
MODEL = Path(__file__).resolve().parents[1] / "vision" / "models" / "hand_landmarker.task"
T0, T1, FPS = 8.0, 18.0, 30.0


def main() -> None:
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 1920
    h = w * 9 // 16
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    hand = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_hands=2,
    ))
    cmd = ["ffmpeg", "-v", "error", "-ss", str(T0), "-t", str(T1 - T0), "-i", str(SRC),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nbytes = w * h * 3
    i = 0
    detected = 0
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        t = T0 + i / FPS
        i += 1
        if i % 3:  # 10fps 샘플링이면 진단에 충분
            continue
        img = mp.Image(image_format=mp.ImageFormat.SRGB,
                       data=np.ascontiguousarray(np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)))
        res = hand.detect_for_video(img, int(t * 1000))
        if not res.hand_landmarks:
            continue
        detected += 1
        for wlm, handed in zip(res.hand_world_landmarks, res.handedness):
            pts = [(p.x, p.y, p.z) for p in wlm]
            wrist = pts[0]
            ratios = [round(math.dist(wrist, pts[tip]) / max(math.dist(wrist, pts[pip]), 1e-9), 2)
                      for pip, tip in _FINGERS]
            palm = max(math.dist(wrist, pts[_MIDDLE_MCP]), 1e-9)
            thumb = round(math.dist(pts[_THUMB_TIP], pts[_MIDDLE_MCP]) / palm, 2)
            print(f"t={t:5.2f} {handed[0].category_name:<5} count={count_extended_fingers(pts)} "
                  f"반경비(검지~새끼)={ratios} 엄지away={thumb:.2f}")
    print(f"-- frames sampled={i // 3} hands detected on {detected}")


if __name__ == "__main__":
    main()
