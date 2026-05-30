"""스파이크 전제 검증용 스크래치 — 클립의 실제 발화 언어 규명.

Slice 2 전사 결과가 영어로 나와, "한국어 클립" 가정이 틀린지 확인한다.
언어-중립 verbatim 프롬프트로 여러 구간을 전사해 어떤 언어가 들리는지 본다.
(정식 테스트 아님 — 1회성 진단. 재현: PYTHONPATH=src .venv/bin/python .dev/probe_clip_language.py)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "src")
from giljobe.llm.vllm_client import default_vllm_client  # noqa: E402
from giljobe.media.window_assembly import assemble_audio_url  # noqa: E402

CLIPS_DIR = Path("/home/kio/workspace/gje")
SR = 16000

# 언어를 강제하지 않고, "발화된 그 언어 그대로" 받아쓰게 한다.
NEUTRAL_SYSTEM = (
    "You are a verbatim speech transcriber. Transcribe the audio EXACTLY as spoken, "
    "in the ORIGINAL language being spoken (do not translate). Preserve filler words. "
    "Output only the transcript text, nothing else. If silent, output empty."
)


def extract(clip: Path, ss: float, dur: float) -> bytes:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(clip),
           "-vn", "-ar", str(SR), "-ac", "1", "-f", "s16le", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=30).stdout


def transcribe_neutral(client, pcm: bytes) -> str:
    resp = client.chat({
        "model": "google/gemma-4-E4B-it",
        "messages": [
            {"role": "system", "content": NEUTRAL_SYSTEM},
            {"role": "user", "content": [
                {"type": "audio_url", "audio_url": {"url": assemble_audio_url(pcm, sample_rate=SR)}},
                {"type": "text", "text": "Transcribe."},
            ]},
        ],
        "max_tokens": 256,
        "temperature": 0.0,
    })
    return resp["choices"][0]["message"]["content"].strip()


def main() -> None:
    client = default_vllm_client()
    # clip 경로 + 총길이를 인자로(없으면 기존 영어 클립). 전체를 5s 윈도우로 훑는다.
    if len(sys.argv) >= 3:
        plan = {sys.argv[1]: float(sys.argv[2])}
    else:
        plan = {str(CLIPS_DIR / "vid_0001.mp4"): 43.0, str(CLIPS_DIR / "vid_0033.mp4"): 50.0}
    for path, dur_total in plan.items():
        clip = Path(path)
        print(f"\n========== {clip.name} ({dur_total:g}s) — neutral verbatim ==========")
        ss = 0.0
        while ss < dur_total:
            d = min(5.0, dur_total - ss)
            pcm = extract(clip, ss, d)
            text = transcribe_neutral(client, pcm)
            print(f"[{ss:5.1f}-{ss + d:5.1f}s] {text!r}")
            ss += 5.0


if __name__ == "__main__":
    main()
