"""Py-Feat marginal-gain 비교 (.dev 스크래치) — MediaPipe ARKit 블렌드셰이프 대비
캘리브레이션 FACS AU + 감정 분류가 변별력을 더하는지, GPU에서 어느 cadence까지 따라오는지.

★ MediaPipe가 못 주는 것만 본다:
  - 진짜(Duchenne) 미소 = AU6(볼 올림)+AU12(입꼬리). MediaPipe mouthSmile의 발화 혼동을 가를 수 있나?
  - "긴장" 후보 AU = AU4(눈썹 내림)·AU14(보조개)·AU23(입술 조임)·AU24(입술 눌림).
  - 감정 분류(happiness/neutral/...) — 별도 채널.
실시간성: GPU에서 frame당 지연 측정(워밍업 제외). 리서치상 ~1-2s/img → dense 30fps 가능성 검증.

재현: `CUDA_VISIBLE_DEVICES=0 .dev/.venv-vision/bin/python .dev/vision/pyfeat_compare.py`  (GPU 사용, vLLM 불요)
★ 주의: venv는 .dev/.venv-vision (이동 금지 — 절대경로 박힘). 멀티GPU에서 CUDA_VISIBLE_DEVICES=0 필수(DataParallel 버그 회피).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent          # .dev/vision/
CLIP = HERE.parent.parent / "kor.mp4"  # repo 루트(gitignore, 사용자 제공)
MP_JSON = HERE / "vision_grounding_poc.json"
GEMMA = HERE.parent / "kor_gemma_out.json"  # 메인 트랙 산출물(.dev/) — 비교 정답지
FRAMES_DIR = HERE / "frames" / "pyfeat"
OUT = HERE / "pyfeat_compare.json"

# 윈도우별 3프레임(ss+0.5/1.5/2.5) — Py-Feat ~1-2s/img라 dense 불가, 윈도우 대표 샘플
OFFSETS = (0.5, 1.5, 2.5)


def extract_frames() -> list[tuple[int, float, Path]]:
    """각 3s 윈도우에서 대표 프레임 추출 → (ss, t, path) 리스트."""
    FRAMES_DIR.mkdir(exist_ok=True)
    out = []
    for ss in range(0, 37, 3):
        for off in OFFSETS:
            t = ss + off
            if t >= 38.0:
                continue
            p = FRAMES_DIR / f"t{t:05.1f}.jpg"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{t:g}", "-i", str(CLIP), "-frames:v", "1", "-q:v", "3", str(p)],
                capture_output=True, timeout=30)
            out.append((ss, t, p))
    return out


def main() -> None:
    from feat import Detector

    frames = extract_frames()
    paths = [str(p) for _, _, p in frames]
    print(f"프레임 {len(paths)}개 추출. Detector 초기화(cuda)…")

    t0 = time.perf_counter()
    detector = Detector(device="cuda")
    init_s = time.perf_counter() - t0
    print(f"Detector init {init_s:.1f}s")

    # 워밍업(모델 그래프 cold 비용 제외)
    t0 = time.perf_counter()
    _ = _detect(detector, [paths[0]])
    warm_s = time.perf_counter() - t0

    # 프레임 1장씩 추론 — 멀티GPU DataParallel 배치 이슈 회피 + per-frame 지연 정확 측정
    per_ms: list[float] = []
    rows = []
    for ss, t, p in frames:
        t0 = time.perf_counter()
        fex = _detect(detector, [str(p)])
        per_ms.append((time.perf_counter() - t0) * 1000)
        aus, emos, poses = _df(fex, "aus"), _df(fex, "emotions"), _df(fex, "poses")
        rows.append({"ss": ss, "t": t,
                     "au": {k: _val(aus, 0, k) for k in ["AU04", "AU06", "AU12", "AU14", "AU23", "AU24"]},
                     "emo": {k: _val(emos, 0, k) for k in ["happiness", "neutral", "sadness", "surprise"]},
                     "yaw": _val(poses, 0, "Yaw"), "pitch": _val(poses, 0, "Pitch")})
    per_frame_ms = float(np.mean(per_ms))

    # MediaPipe + Gemma 정렬
    mp_win = {w["ss"]: w for w in json.loads(MP_JSON.read_text())["windows"]}
    gemma = {r["ss"]: r["nv_parsed"]["note"] for r in json.loads(GEMMA.read_text())["nv_grid"]}

    windows = []
    for ss in range(0, 37, 3):
        wr = [r for r in rows if r["ss"] == ss]
        if not wr:
            continue
        def m(path_a, path_b):
            vals = [r[path_a][path_b] for r in wr if r[path_a][path_b] is not None]
            return round(float(np.mean(vals)), 3) if vals else None
        windows.append({
            "ss": ss,
            "AU12_smile": m("au", "AU12"), "AU06_cheek": m("au", "AU06"),
            "AU04_browlow": m("au", "AU04"), "AU14_dimple": m("au", "AU14"),
            "AU23_tight": m("au", "AU23"), "AU24_press": m("au", "AU24"),
            "happy": m("emo", "happiness"), "neutral": m("emo", "neutral"),
            "mp_smile_mean": mp_win.get(ss, {}).get("smile_mean"),
            "mp_smile_max": mp_win.get(ss, {}).get("smile_max"),
            "gemma_note": gemma.get(ss),
        })

    record = {
        "clip": CLIP.name, "frames": len(paths),
        "realtime": {"init_s": round(init_s, 1), "warmup_s": round(warm_s, 2),
                     "per_frame_ms": round(per_frame_ms, 1),
                     "throughput_fps": round(1000 / per_frame_ms, 2),
                     "realtime_30fps": per_frame_ms < (1000 / 30)},
        "windows": windows,
    }
    OUT.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _report(record)
    print(f"\n저장: {OUT}")


def _detect(detector, paths):
    if hasattr(detector, "detect_image"):
        return detector.detect_image(paths)
    return detector.detect(paths, data_type="image")


def _df(fex, accessor):
    try:
        return getattr(fex, accessor)
    except Exception:  # noqa: BLE001
        return fex


def _val(df, i, col):
    try:
        if col in df.columns:
            return float(df.iloc[i][col])
    except Exception:  # noqa: BLE001
        pass
    return None


def _report(r: dict) -> None:
    rt = r["realtime"]
    print("=" * 110)
    print(f"Py-Feat (GPU) — {r['frames']}프레임")
    print(f"[실시간성] init {rt['init_s']}s · warmup {rt['warmup_s']}s · {rt['per_frame_ms']}ms/frame "
          f"= {rt['throughput_fps']}fps · 30fps실시간? {rt['realtime_30fps']}")
    print("=" * 110)
    print(f"{'win':>4} {'AU12':>5} {'AU06':>5} {'AU04':>5} {'AU14':>5} {'AU23':>5} {'AU24':>5} "
          f"{'happy':>5} {'neutr':>5} | {'mpSmile':>7} | 젬마 nv_note")
    for w in r["windows"]:
        def g(k): return f"{w[k]:.2f}" if w.get(k) is not None else "  - "
        print(f"{w['ss']:>3}s {g('AU12_smile'):>5} {g('AU06_cheek'):>5} {g('AU04_browlow'):>5} "
              f"{g('AU14_dimple'):>5} {g('AU23_tight'):>5} {g('AU24_press'):>5} "
              f"{g('happy'):>5} {g('neutral'):>5} | {str(w.get('mp_smile_mean')):>4}/{str(w.get('mp_smile_max')):<4} | {w['gemma_note']}")


if __name__ == "__main__":
    main()
