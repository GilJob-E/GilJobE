"""포즈/제스처 그라운딩 PoC (.dev 스크래치) — MediaPipe Pose + Hand Landmarker를 kor.mp4
**전 프레임 30fps**로 돌려, 젬마 eval이 근거 없이 반복하는 "제스처 없음·경직된 자세·손동작
없음"을 객관 수치(어깨/몸통 움직임, 손목 변위, 손 검출률)로 검증한다.

★ 이 신호는 ②각성/표정과 달리 **기하학적으로 객관**(움직임/정지를 좌표로 측정 → Barrett 무관).
★ 가설: 이 영상은 타이트 상반신 샷이라 **손이 프레임 밖** → "손동작 없음"은 젬마가 *관측 못 한
   걸 사실로 단정*한 것일 수 있음. visibility 필드로 이걸 직접 드러낸다.

재현: `.venv/bin/python .dev/vision/pose_grounding_poc.py`  (CPU only, GPU/vLLM 불요)
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

HERE = Path(__file__).parent          # .dev/vision/
CLIP = HERE.parent.parent / "kor.mp4"  # repo 루트(gitignore, 사용자 제공)
POSE_MODEL = HERE / "models" / "pose_landmarker.task"
HAND_MODEL = HERE / "models" / "hand_landmarker.task"
GEMMA = HERE.parent / "kor_gemma_out.json"  # 메인 트랙 산출물(.dev/) — 비교 정답지
OUT = HERE / "pose_grounding_poc.json"

FPS = 30.0
WINDOW_S = 3.0
# Pose 33 랜드마크 인덱스
NOSE, LSH, RSH, LEL, REL, LWR, RWR = 0, 11, 12, 13, 14, 15, 16
VIS_TH = 0.5  # visibility 임계(프레임 안/밖 판단)


def main() -> None:
    pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(POSE_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_poses=1))
    hand = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(HAND_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_hands=2))

    cap = cv2.VideoCapture(str(CLIP))
    frames: list[dict] = []
    infer_ms: list[float] = []
    idx = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts = int(idx * 1000.0 / FPS)
        t0 = time.perf_counter()
        pres = pose.detect_for_video(mp_img, ts)
        hres = hand.detect_for_video(mp_img, ts)
        infer_ms.append((time.perf_counter() - t0) * 1000.0)
        t = idx / FPS
        row: dict = {"t": t, "pose": False, "hands": len(hres.hand_landmarks)}
        if pres.pose_landmarks:
            lm = pres.pose_landmarks[0]
            row["pose"] = True
            row["vis"] = {k: lm[k].visibility for k in (LSH, RSH, LWR, RWR)}
            row["sh_mid"] = ((lm[LSH].x + lm[RSH].x) / 2, (lm[LSH].y + lm[RSH].y) / 2)
            row["sh_w"] = math.dist((lm[LSH].x, lm[LSH].y), (lm[RSH].x, lm[RSH].y))  # 어깨폭(스케일 정규화용)
            row["nose"] = (lm[NOSE].x, lm[NOSE].y)
            row["wr"] = {LWR: (lm[LWR].x, lm[LWR].y), RWR: (lm[RWR].x, lm[RWR].y)}
        frames.append(row)
        idx += 1
    cap.release(); pose.close(); hand.close()

    detected = [f for f in frames if f["pose"]]
    throughput = idx / (sum(infer_ms) / 1000.0)
    hand_rate = sum(1 for f in frames if f["hands"] > 0) / idx
    # 어깨가 보이는 프레임 비율 / 손목이 보이는 프레임 비율 (프레임 안/밖)
    sh_vis_rate = np.mean([(f["vis"][LSH] > VIS_TH and f["vis"][RSH] > VIS_TH) for f in detected])
    wr_vis_rate = np.mean([(f["vis"][LWR] > VIS_TH or f["vis"][RWR] > VIS_TH) for f in detected])

    gemma_nv = {r["ss"]: r["nv_parsed"]["note"] for r in json.loads(GEMMA.read_text())["nv_grid"]}
    gemma_eval = {e["ss"]: e["parsed"].get("visual", {}) for e in json.loads(GEMMA.read_text())["eval"]}

    windows = []
    for ss in range(0, 37, 3):
        wf = [f for f in detected if ss <= f["t"] < ss + WINDOW_S]
        if len(wf) < 2:
            continue
        # 스케일 정규화: 위치를 어깨폭으로 나눠 카메라 거리 무관하게
        sw = float(np.mean([f["sh_w"] for f in wf])) or 1e-6
        sh_mid = np.array([f["sh_mid"] for f in wf])
        nose = np.array([f["nose"] for f in wf])
        # 자세 안정성: 어깨중점 위치 표준편차(어깨폭 정규화). 낮을수록 정지/안정
        posture_std = float(np.mean(np.std(sh_mid, axis=0))) / sw
        head_sway = float(np.mean(np.std(nose, axis=0))) / sw
        # 손목 움직임(제스처): 손목이 보일 때만, 프레임간 변위(어깨폭 정규화)
        wrist_motion, gesture_events = _wrist_motion(wf, sw)
        windows.append({
            "ss": ss, "frames": len(wf),
            "wrist_visible": round(float(np.mean([(f["vis"][LWR] > VIS_TH or f["vis"][RWR] > VIS_TH) for f in wf])), 2),
            "hands_detected": round(float(np.mean([f["hands"] > 0 for f in wf])), 2),
            "posture_std": round(posture_std, 4), "head_sway": round(head_sway, 4),
            "wrist_motion": round(wrist_motion, 4), "gesture_events": gesture_events,
            "gemma_nv": gemma_nv.get(ss),
        })

    record = {
        "clip": CLIP.name, "fps": FPS, "total_frames": idx,
        "pose_detect_rate": round(len(detected) / idx, 3),
        "shoulder_visible_rate": round(float(sh_vis_rate), 3),
        "wrist_visible_rate": round(float(wr_vis_rate), 3),
        "hand_detect_rate": round(hand_rate, 3),
        "realtime": {"avg_infer_ms": round(float(np.mean(infer_ms)), 2),
                     "throughput_fps": round(throughput, 1),
                     "realtime_margin_x": round(throughput / FPS, 1)},
        "gemma_eval_visual": gemma_eval,
        "windows": windows,
    }
    OUT.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _report(record)
    print(f"\n저장: {OUT}")


def _wrist_motion(wf: list[dict], sw: float) -> tuple[float, int]:
    """손목이 보이는 프레임간 변위 평균(어깨폭 정규화) + 제스처 이벤트 수(속도 피크)."""
    vels = []
    for a, b in zip(wf[:-1], wf[1:]):
        ds = []
        for w in (LWR, RWR):
            if a["vis"][w] > VIS_TH and b["vis"][w] > VIS_TH:
                ds.append(math.dist(a["wr"][w], b["wr"][w]))
        if ds:
            vels.append(max(ds) / sw)
    if not vels:
        return 0.0, 0
    events = sum(1 for v in vels if v > 0.15)  # 어깨폭의 15%/frame 이상 = 뚜렷한 손동작
    return float(np.mean(vels)), events


def _report(r: dict) -> None:
    rt = r["realtime"]
    print("=" * 104)
    print(f"MediaPipe Pose+Hand — kor.mp4 @ {r['fps']}fps, {r['total_frames']}프레임")
    print(f"[실시간성] avg {rt['avg_infer_ms']}ms/frame(pose+hand) · {rt['throughput_fps']}fps = {rt['realtime_margin_x']}x")
    print(f"[검출] pose {r['pose_detect_rate']*100:.0f}% · 어깨보임 {r['shoulder_visible_rate']*100:.0f}% · "
          f"손목보임 {r['wrist_visible_rate']*100:.0f}% · 손검출 {r['hand_detect_rate']*100:.0f}%")
    print("=" * 104)
    print(f"{'win':>4} {'wristVis':>8} {'hands':>5} {'postStd':>7} {'headSway':>8} {'wristMot':>8} {'gestEvt':>7}  젬마 nv")
    for w in r["windows"]:
        print(f"{w['ss']:>3}s {w['wrist_visible']:>8} {w['hands_detected']:>5} {w['posture_std']:>7} "
              f"{w['head_sway']:>8} {w['wrist_motion']:>8} {w['gesture_events']:>7}  {w['gemma_nv']}")
    print("\n젬마 eval(시각) 자세/제스처 주장:")
    for ss, v in r["gemma_eval_visual"].items():
        print(f"  [{ss}s] posture: {v.get('posture','')}")
        print(f"        gesture: {v.get('gesture_over_time','')}")


if __name__ == "__main__":
    main()
