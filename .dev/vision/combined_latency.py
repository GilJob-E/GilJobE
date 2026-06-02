"""Face+Pose 동시 실행 레이턴시 실측 (.dev 스크래치) — "레이턴시 문제없다" 확언 전 마지막 측정.

세 구성을 잰다:
  A) face+pose 순차(단일 스레드) — 가장 단순한 통합
  B) face+pose 병렬(스레드 2개) — 모델별 스레드 분리 시
  C) face+pose+hand 순차 — 손 보이는 셋업 대비 최대 비용

재현: `.venv/bin/python .dev/vision/combined_latency.py`  (CPU only)
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

HERE = Path(__file__).parent          # .dev/vision/
CLIP = HERE.parent.parent / "kor.mp4"  # repo 루트(gitignore, 사용자 제공)
MODELS = HERE / "models"
OUT = HERE / "combined_latency.json"
FPS = 30.0
N_FRAMES = 300  # 10초 분량이면 통계 충분


def make_face():
    return vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODELS / "face_landmarker.task")),
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        output_face_blendshapes=True, output_facial_transformation_matrixes=True))


def make_pose():
    return vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODELS / "pose_landmarker.task")),
        running_mode=vision.RunningMode.VIDEO, num_poses=1))


def make_hand():
    return vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODELS / "hand_landmarker.task")),
        running_mode=vision.RunningMode.VIDEO, num_hands=2))


def load_frames(n: int) -> list:
    cap = cv2.VideoCapture(str(CLIP))
    out = []
    while len(out) < n:
        ok, bgr = cap.read()
        if not ok:
            break
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        out.append(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    cap.release()
    return out


def bench(label: str, fn, frames) -> dict:
    # 워밍업 5프레임
    for i in range(5):
        fn(frames[i], i)
    lat = []
    for i, img in enumerate(frames[5:], start=5):
        t0 = time.perf_counter()
        fn(img, i)
        lat.append((time.perf_counter() - t0) * 1000)
    r = {"label": label, "avg_ms": round(float(np.mean(lat)), 2),
         "p95_ms": round(float(np.percentile(lat, 95)), 2),
         "fps": round(1000 / float(np.mean(lat)), 1),
         "margin_x_vs_30fps": round(1000 / float(np.mean(lat)) / 30, 2)}
    print(f"  {label:<34} avg {r['avg_ms']:>6}ms  p95 {r['p95_ms']:>6}ms  "
          f"{r['fps']:>5}fps  ({r['margin_x_vs_30fps']}x)")
    return r


def main() -> None:
    frames = load_frames(N_FRAMES)
    print(f"kor.mp4 앞 {len(frames)}프레임(10s)으로 측정\n")
    results = []

    # A) face+pose 순차
    face, pose = make_face(), make_pose()
    def seq_fp(img, i):
        ts = int(i * 1000 / FPS)
        face.detect_for_video(img, ts); pose.detect_for_video(img, ts)
    results.append(bench("A) face+pose 순차(1스레드)", seq_fp, frames))
    face.close(); pose.close()

    # B) face+pose 병렬(스레드 2)
    face, pose = make_face(), make_pose()
    ex = ThreadPoolExecutor(max_workers=2)
    def par_fp(img, i):
        ts = int(i * 1000 / FPS)
        f1 = ex.submit(face.detect_for_video, img, ts)
        f2 = ex.submit(pose.detect_for_video, img, ts)
        f1.result(); f2.result()
    results.append(bench("B) face+pose 병렬(2스레드)", par_fp, frames))
    ex.shutdown(); face.close(); pose.close()

    # C) face+pose+hand 순차 (손 보이는 셋업 최대 비용)
    face, pose, hand = make_face(), make_pose(), make_hand()
    def seq_fph(img, i):
        ts = int(i * 1000 / FPS)
        face.detect_for_video(img, ts); pose.detect_for_video(img, ts); hand.detect_for_video(img, ts)
    results.append(bench("C) face+pose+hand 순차(1스레드)", seq_fph, frames))
    face.close(); pose.close(); hand.close()

    OUT.write_text(json.dumps({"clip": CLIP.name, "n_frames": len(frames), "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {OUT}")


if __name__ == "__main__":
    main()
