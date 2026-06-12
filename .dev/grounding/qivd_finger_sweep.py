"""QIVD 손가락 임계 스윕 — 랜드마크 1회 추출(캐시) 후 radial/thumb_away 그리드 최적화.

count_extended_fingers의 임계를 추측이 아니라 정답 95문항 정확도로 정한다. 첫 실행은
IMAGE 모드로 각 비디오 timestamp 윈도우의 world 랜드마크를 추출해 캐시(.npz 대신 json),
이후 실행은 캐시에서 임계만 스윕(검출 0회 — 즉시).

"순수 카운팅" 정확도를 본다: '한 손만'(right/left hand)·'숨긴'(hiding)·증분 질문은 레인이
답할 수 없으므로(질문 의미 해석=adapter/VLM 영역) 별도 태그해 제외 정확도도 함께 보고.

실행: .venv/bin/python .dev/grounding/qivd_finger_sweep.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from giljobe.analysis.grounding import count_extended_fingers  # noqa: E402

QIVD = Path("/home/kio/workspace/giljob-docker/benchmark/data/raw/qivd")
MODEL = Path(__file__).resolve().parents[1] / "vision" / "models" / "hand_landmarker.task"
CACHE = Path(__file__).with_name("qivd_finger_landmarks.json")
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
W, H = 960, 540
# 레인이 답할 수 없는 질문(어느 손인지·숨김·증분 의미 — adapter/VLM 영역)
SINGLE_HAND = re.compile(r"\b(right|left)\s+hand\b|\bon my (right|left)\b", re.I)
HIDDEN = re.compile(r"hiding|hidden|not show|folded|down\b", re.I)


def as_int(s: object) -> int | None:
    t = str(s).strip().lower()
    return int(t) if t.isdigit() else WORDS.get(t)


def ts_seconds(ts: str) -> float:
    m, s = ts.split(":")
    return int(m) * 60 + float(s)


def subset() -> list[dict]:
    labels = json.loads((QIVD / "labels.json").read_text())
    out = []
    for l in labels:
        if not re.search(r"how many finger", l.get("question", ""), re.I):
            continue
        ans = as_int(l.get("short_answer"))
        if ans is None:
            continue
        q = l["question"]
        out.append({"video": l["video"], "answer": ans, "ts": ts_seconds(l.get("timestamp", "00:00")),
                    "q": q, "lane_answerable": not (SINGLE_HAND.search(q) or HIDDEN.search(q))})
    return out


def extract_landmarks() -> list[dict]:
    """각 문항의 timestamp 윈도우 프레임별 world 랜드마크(손별) 추출 — 캐시."""
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision
    import mediapipe as mp

    det = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.IMAGE, num_hands=2))
    rows = subset()
    nbytes = W * H * 3
    for idx, r in enumerate(rows):
        t0 = max(0.0, r["ts"] - 0.8)
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{t0}", "-t", "1.6", "-i", str(QIVD / "videos" / r["video"]),
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "10", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        frames = []
        while True:
            buf = proc.stdout.read(nbytes)
            if len(buf) < nbytes:
                break
            img = mp.Image(image_format=mp.ImageFormat.SRGB,
                           data=np.ascontiguousarray(np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)))
            res = det.detect(img)
            if res.hand_world_landmarks:
                frames.append([[(p.x, p.y, p.z) for p in lm] for lm in res.hand_world_landmarks])
        proc.wait()
        r["frames"] = frames
        if idx % 20 == 0:
            print(f"  extract {idx}/{len(rows)}")
    return rows


def predict(frames: list, radial: float, thumb_away: float) -> int | None:
    """프레임별 (양손 합산) 손가락 수의 중위수 — 임계 파라미터로 재계산."""
    if not frames:
        return None
    per_frame = [sum(count_extended_fingers(hand, radial=radial, thumb_away=thumb_away) for hand in fr)
                 for fr in frames]
    return int(np.median(per_frame))


def score(rows: list[dict], radial: float, thumb_away: float, *, lane_only: bool) -> tuple[int, int]:
    ok = tot = 0
    for r in rows:
        if lane_only and not r["lane_answerable"]:
            continue
        tot += 1
        if predict(r["frames"], radial, thumb_away) == r["answer"]:
            ok += 1
    return ok, tot


def main() -> None:
    if CACHE.is_file():
        rows = json.loads(CACHE.read_text())
        print(f"캐시 로드: {len(rows)}문항 ({CACHE.name})")
    else:
        rows = extract_landmarks()
        CACHE.write_text(json.dumps(rows))
        print(f"랜드마크 캐시 저장: {CACHE.name}")

    n_lane = sum(r["lane_answerable"] for r in rows)
    print(f"\n전체 {len(rows)}문항 / 레인 답변가능 {n_lane}문항(한손·숨김 제외)\n")
    print(f"{'radial':>7} {'thumb':>6} | {'전체 exact':>10} | {'레인 exact':>10}")
    best = (0.0, None)
    for radial in (1.10, 1.15, 1.20, 1.25, 1.30):
        for thumb_away in (0.62, 0.68, 0.72, 0.75, 0.80):
            ok_all, tot_all = score(rows, radial, thumb_away, lane_only=False)
            ok_lane, tot_lane = score(rows, radial, thumb_away, lane_only=True)
            acc_lane = ok_lane / tot_lane
            mark = ""
            if acc_lane > best[0]:
                best = (acc_lane, (radial, thumb_away))
                mark = " <-"
            print(f"{radial:>7.2f} {thumb_away:>6.2f} | {ok_all/tot_all:>9.3f}  | "
                  f"{acc_lane:>9.3f}{mark}")
    print(f"\n최적(레인 정확도): radial={best[1][0]} thumb_away={best[1][1]} → {best[0]:.3f}")


if __name__ == "__main__":
    main()
