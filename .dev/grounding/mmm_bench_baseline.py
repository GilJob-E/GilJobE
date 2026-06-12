"""MMM 기준 파일 베이스라인 — 현행 레인(프로소디 + 비전 face/pose)이 뭘 잡는지 측정.

기준 파일(giljob-docker/qa/, gitignored 원시 미디어):
  - .audio_test.mp4: 속삭이며 느린 발화 → 프로소디 변별(voiced_ratio·조음속도) 확인
  - .vision_test.mp4: 손가락 3→2→5 → 현 face/pose 레인은 손가락 미지원(갭 확인용)

실행: GILJOBE_VISION_MODELS_DIR=.dev/vision/models .venv/bin/python .dev/grounding/mmm_bench_baseline.py
출력: 같은 폴더 mmm_bench_baseline.json + stdout 요약.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from giljobe.analysis.grounding import describe_visual, maybe_vision_grounder
from giljobe.analysis.prosody import ProsodyExtractor, describe_vocal

QA = Path("/home/kio/workspace/giljob-docker/qa")
FILES = {"audio_test": QA / ".audio_test.mp4", "vision_test": QA / ".vision_test.mp4"}
W, H, FPS = 960, 540, 30.0  # 프로덕션 웹캠 해상도 근사(원본 1080p를 다운스케일)


def pcm_16k_mono(path: Path) -> bytes:
    """ffmpeg로 16k mono Int16 PCM 추출 — 레인 입력 규격과 동일."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "1", "-ar", "16000", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def iter_frames(path: Path):
    """ffmpeg rgb24 파이프 → (t_s, ndarray) 제너레이터. 프레임 단위 스트리밍(메모리 상한)."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nbytes = W * H * 3
    i = 0
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        yield i / FPS, np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)
        i += 1
    proc.wait()


def run_vision(path: Path) -> dict:
    g = maybe_vision_grounder()
    if g is None:
        return {"error": "vision lane unavailable"}
    t0 = time.time()
    n = 0
    last_t = 0.0
    for t_s, frame in iter_frames(path):
        g.add_frame(t_s, frame)
        n += 1
        last_t = t_s
        if n % 30 == 0:
            g.wait_idle(timeout=30)  # 벤치마크는 완전성 우선 — load-shedding 방지 배리어
    g.wait_idle(timeout=60)
    m = g.window_metrics(0.0, last_t + 1.0) or {}
    g.close()
    m["_frames_fed"] = n
    m["_wall_s"] = round(time.time() - t0, 1)
    return m


def run_prosody(path: Path) -> dict:
    pcm = pcm_16k_mono(path)
    m = ProsodyExtractor().extract(pcm)
    return m if m is not None else {"error": "extract returned None"}


def main() -> None:
    out: dict = {}
    for name, path in FILES.items():
        print(f"=== {name} ({path.name}) ===")
        prosody = run_prosody(path)
        vision = run_vision(path)
        out[name] = {"prosody": prosody, "vision": vision}
        print("--- describe_vocal ---")
        print(describe_vocal(prosody) if "error" not in prosody else prosody["error"])
        print("--- describe_visual ---")
        print(describe_visual(vision) if "error" not in vision else vision["error"])
        print()
    dst = Path(__file__).with_suffix(".json")
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"saved -> {dst}")


if __name__ == "__main__":
    main()
