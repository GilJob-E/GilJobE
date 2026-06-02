"""비전 그라운딩 PoC (.dev 스크래치) — MediaPipe Face Landmarker를 kor.mp4 **전 프레임
30fps**로 돌려, 젬마가 3s cadence로는 구조적으로 못 내는 시계열 신호(깜빡임 빈도·시선
온카메라%·미소 지속·머리 흔들림·입술 모션)를 뽑는다.

★ 설계 원칙(반성 반영): 이건 "같은 입력 주고 누가 맞나" 벤치마크가 아니라 **협력**이다.
싼 CPU 레인을 네이티브 fps로 풀가동해, 비싼 젬마(3s)가 못 보는 걸 채워 그라운딩한다.
그래서 fps를 젬마에 맞춰 굶기지 않는다.

두 평가 기준:
  1) 실시간성 — frames/sec throughput이 30fps(라이브)를 넘는가.
  2) 젬마 보조 충분성 — 젬마의 자기모순/상투구(kor_gemma_out.json)를 객관 수치가 심판/보강하는가.

재현: `.venv/bin/python .dev/vision/vision_grounding_poc.py`  (vLLM·GPU 불요, CPU only)
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
MODEL = HERE / "models" / "face_landmarker.task"
GEMMA = HERE.parent / "kor_gemma_out.json"  # 메인 트랙 산출물(.dev/) — 비교 정답지
OUT = HERE / "vision_grounding_poc.json"

FPS = 30.0
WINDOW_S = 3.0
# 관심 블렌드셰이프(ARKit 52종 중 면접 비언어 관련만)
BS_KEYS = [
    "mouthSmileLeft", "mouthSmileRight", "mouthPressLeft", "mouthPressRight",
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "eyeBlinkLeft", "eyeBlinkRight", "jawOpen",
    "eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft", "eyeLookDownLeft",
    "eyeLookInRight", "eyeLookOutRight", "eyeLookUpRight", "eyeLookDownRight",
]
# 입술 모션("떨림") 프록시용 랜드마크: 입꼬리(61,291)·윗입술(13)·아랫입술(14)
LIP_LMK = [61, 291, 13, 14]


def euler_deg(mat: np.ndarray) -> tuple[float, float, float]:
    """4x4 변환행렬 회전부 → (yaw, pitch, roll) 도. 절대각 부호는 MediaPipe 관례라
    그라운딩엔 분산(흔들림)이 더 중요 — 부호보다 변동성을 본다."""
    r = mat[:3, :3]
    sy = math.sqrt(r[0, 0] ** 2 + r[1, 0] ** 2)
    pitch = math.degrees(math.atan2(-r[2, 0], sy))
    yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    roll = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return yaw, pitch, roll


def build_landmarker() -> vision.FaceLandmarker:
    opts = vision.FaceLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )
    return vision.FaceLandmarker.create_from_options(opts)


def main() -> None:
    landmarker = build_landmarker()
    cap = cv2.VideoCapture(str(CLIP))
    frames: list[dict] = []  # per-frame 신호
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
        res = landmarker.detect_for_video(mp_img, ts)
        infer_ms.append((time.perf_counter() - t0) * 1000.0)
        t = idx / FPS
        if res.face_blendshapes:
            bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
            yaw, pitch, roll = euler_deg(np.array(res.facial_transformation_matrixes[0]))
            lmk = res.face_landmarks[0]
            lips = {i: (lmk[i].x, lmk[i].y) for i in LIP_LMK}
            frames.append({"t": t, "face": True, "bs": {k: bs.get(k, 0.0) for k in BS_KEYS},
                           "yaw": yaw, "pitch": pitch, "roll": roll, "lips": lips})
        else:
            frames.append({"t": t, "face": False})
        idx += 1
    cap.release()
    landmarker.close()

    detected = [f for f in frames if f["face"]]
    n_face = len(detected)
    throughput = idx / (sum(infer_ms) / 1000.0)

    # --- 깜빡임: avg(eyeBlinkL,R)이 0.5를 상향 교차한 횟수(전체) ---
    BLINK_TH = 0.5
    blink_series = [(f["t"], (f["bs"]["eyeBlinkLeft"] + f["bs"]["eyeBlinkRight"]) / 2) for f in detected]
    blinks = []
    prev = 0.0
    for t, v in blink_series:
        if prev < BLINK_TH <= v:
            blinks.append(round(t, 2))
        prev = v
    dur_min = (idx / FPS) / 60.0
    blink_rate = len(blinks) / dur_min if dur_min else 0.0

    # --- Gemma 3s 그리드에 정렬 ---
    gemma = {r["ss"]: r["nv_parsed"]["note"] for r in json.loads(GEMMA.read_text())["nv_grid"]}
    windows = []
    for ss in range(0, 37, 3):
        wf = [f for f in detected if ss <= f["t"] < ss + WINDOW_S]
        if not wf:
            windows.append({"ss": ss, "face_frames": 0, "gemma_note": gemma.get(ss)})
            continue
        smile = [(f["bs"]["mouthSmileLeft"] + f["bs"]["mouthSmileRight"]) / 2 for f in wf]
        brow_down = [(f["bs"]["browDownLeft"] + f["bs"]["browDownRight"]) / 2 for f in wf]
        press = [(f["bs"]["mouthPressLeft"] + f["bs"]["mouthPressRight"]) / 2 for f in wf]
        blink = [(f["bs"]["eyeBlinkLeft"] + f["bs"]["eyeBlinkRight"]) / 2 for f in wf]
        # 시선 이탈 프록시: 눈동자 좌우상하 이동량 최대 + 머리 yaw/pitch 편차
        gaze_off = [max(f["bs"]["eyeLookOutLeft"], f["bs"]["eyeLookOutRight"],
                        f["bs"]["eyeLookInLeft"], f["bs"]["eyeLookInRight"],
                        f["bs"]["eyeLookUpLeft"], f["bs"]["eyeLookDownLeft"]) for f in wf]
        yaw = [f["yaw"] for f in wf]
        pitch = [f["pitch"] for f in wf]
        nblink = sum(1 for t in blinks if ss <= t < ss + WINDOW_S)
        # 입술 모션(떨림 프록시): 프레임간 입술 랜드마크 이동 속도의 평균(정규화 좌표)
        lip_vel = _lip_velocity(wf)
        windows.append({
            "ss": ss, "face_frames": len(wf),
            "smile_mean": round(float(np.mean(smile)), 3), "smile_max": round(float(np.max(smile)), 3),
            "smile_pct>0.2": round(float(np.mean([s > 0.2 for s in smile])), 2),
            "brow_down_mean": round(float(np.mean(brow_down)), 3),
            "mouth_press_mean": round(float(np.mean(press)), 3),
            "blink_in_window": nblink,
            "gaze_off_mean": round(float(np.mean(gaze_off)), 3),
            "yaw_std": round(float(np.std(yaw)), 2), "pitch_std": round(float(np.std(pitch)), 2),
            "lip_motion": round(lip_vel, 5),
            "gemma_note": gemma.get(ss),
        })

    record = {
        "clip": CLIP.name, "fps": FPS, "total_frames": idx, "face_detected_frames": n_face,
        "face_detect_rate": round(n_face / idx, 3),
        "realtime": {
            "avg_infer_ms": round(float(np.mean(infer_ms)), 2),
            "p95_infer_ms": round(float(np.percentile(infer_ms, 95)), 2),
            "throughput_fps": round(throughput, 1),
            "realtime_margin_x": round(throughput / FPS, 1),
        },
        "blink_overall": {"count": len(blinks), "times": blinks, "rate_per_min": round(blink_rate, 1)},
        "windows": windows,
    }
    OUT.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_report(record)
    print(f"\n저장: {OUT}")


def _lip_velocity(wf: list[dict]) -> float:
    """창 내 프레임간 입술 랜드마크 평균 이동량(정규화). 1초 간격 3프레임으론 0에 수렴하는
    '떨림'을 30fps에선 실제로 측정 — 젬마 상투구('입술 떨림') 검증용."""
    if len(wf) < 2:
        return 0.0
    vels = []
    for a, b in zip(wf[:-1], wf[1:]):
        d = [math.dist(a["lips"][i], b["lips"][i]) for i in LIP_LMK]
        vels.append(sum(d) / len(d))
    return float(np.mean(vels))


def _print_report(r: dict) -> None:
    rt = r["realtime"]
    print("=" * 100)
    print(f"MediaPipe Face Landmarker — kor.mp4 @ {r['fps']}fps, {r['total_frames']}프레임, "
          f"얼굴검출 {r['face_detect_rate']*100:.0f}%")
    print(f"[실시간성] avg {rt['avg_infer_ms']}ms/frame · p95 {rt['p95_infer_ms']}ms · "
          f"throughput {rt['throughput_fps']}fps = 라이브 30fps 대비 {rt['realtime_margin_x']}x")
    bo = r["blink_overall"]
    print(f"[깜빡임] 전체 {bo['count']}회 / {bo['rate_per_min']}회·분  (젬반 3s cadence로는 측정 불가 신호)")
    print("=" * 100)
    print(f"{'win':>5} {'smile':>11} {'smile%':>6} {'brow↓':>6} {'press':>6} {'blink':>5} "
          f"{'gazeOff':>7} {'yaw_s':>6} {'lipMot':>7}  젬마 nv_note")
    for w in r["windows"]:
        if not w.get("face_frames"):
            print(f"{w['ss']:>4}s  (no face)   {w.get('gemma_note','')}")
            continue
        print(f"{w['ss']:>4}s {w['smile_mean']:>5}/{w['smile_max']:<5} {w['smile_pct>0.2']:>6} "
              f"{w['brow_down_mean']:>6} {w['mouth_press_mean']:>6} {w['blink_in_window']:>5} "
              f"{w['gaze_off_mean']:>7} {w['yaw_std']:>6} {w['lip_motion']:>7}  {w['gemma_note']}")


if __name__ == "__main__":
    main()
