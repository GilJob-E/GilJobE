"""QIVD 손가락 카운팅 subset으로 hands 레인 정확도 실측 — .vision_test 1개를 벤치 95개로 확장.

QIVD object_counting "How many fingers…" 95문항(정수 답 0~10, 양손 포함)을 GilJobE hands
레인에 통과시켜 정확도·혼동을 잰다. 정적 제스처라 finger_segments(전이 붕괴)가 아니라
answer timestamp 부근 윈도우의 손가락 수 중위수를 예측으로 쓴다.

실행: .venv/bin/python .dev/grounding/qivd_finger_eval.py [limit]
출력: 같은 폴더 qivd_finger_eval.json + stdout(정확도·±1 정확도·혼동 상위).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from giljobe.analysis.grounding import count_extended_fingers  # noqa: E402

QIVD = Path("/home/kio/workspace/giljob-docker/benchmark/data/raw/qivd")
MODEL = Path(__file__).resolve().parents[1] / "vision" / "models" / "hand_landmarker.task"
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
W, H = 960, 540


def as_int(s: object) -> int | None:
    t = str(s).strip().lower()
    return int(t) if t.isdigit() else WORDS.get(t)


def ts_seconds(ts: str) -> float:
    """'00:03.9' → 3.9 초."""
    m, s = ts.split(":")
    return int(m) * 60 + float(s)


def subset() -> list[dict]:
    labels = json.loads((QIVD / "labels.json").read_text())
    out = []
    for l in labels:
        if not re.search(r"how many finger", l.get("question", ""), re.I):
            continue
        ans = as_int(l.get("short_answer"))
        if ans is not None:
            out.append({"video": l["video"], "answer": ans,
                        "ts": ts_seconds(l.get("timestamp", "00:00")), "q": l["question"]})
    return out


def predict(detector, video: Path, ts: float) -> int | None:
    """answer timestamp ±0.8s 윈도우를 10fps로 hands 레인에 넣어 손가락 수 중위수.
    정적 제스처라 IMAGE 모드 — 프레임 독립 재검출(VIDEO 추적 환각 회피, 단조 ts 불요)."""
    import mediapipe as mp

    t0 = max(0.0, ts - 0.8)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t0}", "-t", "1.6", "-i", str(video),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "10", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nbytes = W * H * 3
    counts = []
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        img = mp.Image(image_format=mp.ImageFormat.SRGB,
                       data=np.ascontiguousarray(np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)))
        res = detector.detect(img)
        if res.hand_world_landmarks:
            counts.append(sum(count_extended_fingers([(p.x, p.y, p.z) for p in lm])
                              for lm in res.hand_world_landmarks))
    proc.wait()
    return int(np.median(counts)) if counts else None


def main() -> None:
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    rows = subset()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(rows)
    rows = rows[:limit]
    det = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.IMAGE, num_hands=2))

    results, exact, within1, nohand = [], 0, 0, 0
    confusion = Counter()
    for r in rows:
        pred = predict(det, QIVD / "videos" / r["video"], r["ts"])
        ok = pred == r["answer"]
        exact += ok
        if pred is None:
            nohand += 1
        else:
            within1 += abs(pred - r["answer"]) <= 1
            confusion[(r["answer"], pred)] += 1
        results.append({**r, "pred": pred, "exact": ok})

    n = len(rows)
    summary = {
        "n": n, "exact": exact, "exact_acc": round(exact / n, 3),
        "within1": within1, "within1_acc": round(within1 / n, 3),
        "no_hand_detected": nohand,
    }
    print("== QIVD finger-count (hands lane) ==")
    print(summary)
    print("\n오답 혼동(정답→예측) 상위:")
    for (gt, pr), c in confusion.most_common(12):
        if gt != pr:
            print(f"  정답 {gt} → 예측 {pr}: {c}회")
    dst = Path(__file__).with_suffix(".json")
    dst.write_text(json.dumps({"summary": summary,
                               "confusion": {f"{gt}->{pr}": c for (gt, pr), c in confusion.items()},
                               "results": results}, ensure_ascii=False, indent=2))
    print(f"\nsaved -> {dst}")


if __name__ == "__main__":
    main()
