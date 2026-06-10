"""inject 스파이크 — "수치를 줘도 젬마가 따르는가?" (비전·오디오 README의 acceptance test).

kor.mp4의 eval 3윈도우 + nv 13윈도우를 **객관 수치 주입 버전**으로 재추론하고, 베이스라인
(.dev/kor_gemma_out.json — 주입 없던 메인 트랙 풀 추론)의 충돌 주장과 병기해 저장한다.
판정 기준: 비전 충돌(입술 떨림 상투구·제스처 단정·ss36 긴장) + vocal 충돌(단조 3/3·속도
자기모순·볼륨 역상관)이 주입 후 사라지는가. "호흡 짧음"은 그라운딩된 주장이라 남아도 정상.

실행: vLLM E4B(localhost:8000) 기동 후
  PYTHONPATH=src .venv/bin/python .dev/grounding/inject_spike.py
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import av
from PIL import Image

from giljobe.analysis.critic import WindowCritic
from giljobe.analysis.grounding import VisionGrounder, describe_visual
from giljobe.analysis.prosody import ProsodyExtractor, describe_vocal
from giljobe.llm.vllm_client import default_vllm_client

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CLIP = ROOT / "kor.mp4"
PCM = ROOT / ".dev" / "audio" / "kor_16k_mono.pcm"
GEMMA = ROOT / ".dev" / "kor_gemma_out.json"
MODELS = ROOT / ".dev" / "vision" / "models"
OUT = HERE / "inject_spike.json"

SR = 16000
EVAL_WINDOWS = [(0, 16), (16, 16), (21, 16)]


def jpeg_640(frame: av.VideoFrame) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(frame.to_ndarray(format="rgb24")).resize((640, 480)).save(
        buf, format="JPEG", quality=85
    )
    return buf.getvalue()


def main() -> None:
    client = default_vllm_client()
    client.health()  # 다운이면 여기서 실패(명확히)

    # ① dense 비전 레인: 전 프레임 30fps + 1fps JPEG(라이브 ingest 규격) 동시 수집
    grounder = VisionGrounder(MODELS / "face_landmarker.task", MODELS / "pose_landmarker.task")
    jpegs: dict[int, bytes] = {}
    n = 0
    with av.open(str(CLIP)) as c:
        for frame in c.decode(video=0):
            t = float(frame.time)
            grounder.add_frame(t, frame.to_ndarray(format="rgb24"))
            sec = int(t)
            if sec not in jpegs:
                jpegs[sec] = jpeg_640(frame)
            n += 1
            if n % 30 == 0:
                grounder.wait_idle()
    grounder.wait_idle()
    print(f"dense 레인: {n}프레임 처리")

    raw = PCM.read_bytes()
    prosody = ProsodyExtractor()
    gemma = json.loads(GEMMA.read_text())
    critic = WindowCritic(client=client)
    out: dict = {"clip": CLIP.name, "eval": [], "nv": []}

    # ② eval 3윈도우 — prosody+vision 주입 재추론 vs 베이스라인 vocal/visual
    for (ss, dur), base in zip(EVAL_WINDOWS, gemma["eval"]):
        pm = prosody.extract(raw[ss * SR * 2 : (ss + dur) * SR * 2])
        vm = grounder.window_metrics(ss, ss + dur)
        frames = [jpegs[s] for s in range(ss, min(ss + dur, max(jpegs) + 1)) if s in jpegs]
        t0 = time.perf_counter()
        sig = critic.evaluate_window(
            frames, raw[ss * SR * 2 : (ss + dur) * SR * 2],
            window_start_s=ss, window_dur_s=dur, prosody=pm, vision=vm,
        )
        out["eval"].append({
            "window": f"{ss}-{ss + dur}s",
            "latency_s": round(time.perf_counter() - t0, 2),
            "baseline_vocal": base["parsed"].get("vocal"),
            "baseline_visual": base["parsed"].get("visual"),
            "injected_vocal": sig.vocal,
            "injected_visual": sig.visual,
            "injected_critique": sig.critique,
            "objective_vocal_labels": describe_vocal(pm),
            "objective_visual_labels": describe_visual(vm),
        })
        print(f"eval {ss}-{ss + dur}s done")

    # ③ nv 13윈도우(3s) — vision 주입 재추론 vs 베이스라인 note(입술 떨림 상투구·ss36 긴장)
    for row in gemma["nv_grid"]:
        ss = row["ss"]
        vm = grounder.window_metrics(ss, ss + 3)
        frames = [jpegs[s] for s in range(ss, ss + 3) if s in jpegs]
        if not frames:
            continue
        sig = critic.read_nonverbal(
            frames, raw[ss * SR * 2 : (ss + 3) * SR * 2], t=ss + 1.5, window_s=3.0, vision=vm,
        )
        out["nv"].append({
            "ss": ss,
            "baseline": row["nv_parsed"],
            "injected": {"state": sig.state, "intensity": sig.intensity, "note": sig.note},
        })
        print(f"nv {ss}s done")

    grounder.close()
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
