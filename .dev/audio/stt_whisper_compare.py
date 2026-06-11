"""faster-whisper ↔ GemmaNative 전사 비교 하니스 (2026-06-11 대개편 #1).

목적: kor.mp4 기준으로 whisper 폴백 채택/모델 선택 근거 수집.
- 프로덕션 제약과 동일한 조건이 핵심: analysis-engine 컨테이너는 CPU 전용 →
  CPU int8 수치가 곧 폴백의 실 레이턴시다.
- 3s 윈도우 모드 = 프로덕션 그리드(윈도우 간 컨텍스트 없음, gemma와 동일 조건).
  full-clip 모드 = 품질 상한 참고치.
- gemma 측 데이터는 QA 스택 실턴 산출물(giljob-docker/qa/kor-signals.json)을 읽는다
  (같은 클립, 같은 3s 그리드 — 재호출 없이 공정 비교).

실행: .venv/bin/python .dev/audio/stt_whisper_compare.py
출력: /home/kio/workspace/giljob-docker/qa/stt-comparison.json
"""
from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np

WAV = Path("/tmp/qa-media/kor16k.wav")
GEMMA_SIGNALS = Path("/home/kio/workspace/giljob-docker/qa/kor-signals.json")
OUT = Path("/home/kio/workspace/giljob-docker/qa/stt-comparison.json")
WINDOW_S = 3.0
MODELS = ["small", "large-v3-turbo"]  # CPU int8 후보 — 폴백 기본값 선정용


def load_pcm() -> np.ndarray:
    with wave.open(str(WAV), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def windows(audio: np.ndarray) -> list[np.ndarray]:
    step = int(WINDOW_S * 16000)
    return [audio[i : i + step] for i in range(0, len(audio), step)]


def transcribe(model, audio: np.ndarray) -> str:
    segments, _ = model.transcribe(audio, language="ko", beam_size=5, vad_filter=False)
    return " ".join(s.text.strip() for s in segments).strip()


def bench_model(name: str, audio: np.ndarray) -> dict:
    from faster_whisper import WhisperModel

    t0 = time.monotonic()
    model = WhisperModel(name, device="cpu", compute_type="int8")
    load_s = time.monotonic() - t0

    t0 = time.monotonic()
    full_text = transcribe(model, audio)
    full_s = time.monotonic() - t0

    win_texts, win_times = [], []
    for w in windows(audio):
        t0 = time.monotonic()
        win_texts.append(transcribe(model, w))
        win_times.append(round(time.monotonic() - t0, 3))

    return {
        "model": name,
        "device": "cpu/int8",
        "load_s": round(load_s, 2),
        "full_clip": {"text": full_text, "latency_s": round(full_s, 2)},
        "windowed_3s": {
            "texts": win_texts,
            "latency_each_s": win_times,
            "latency_median_s": round(float(np.median(win_times)), 3),
            "latency_max_s": round(max(win_times), 3),
            "joined": " ".join(t for t in win_texts if t),
        },
    }


def main() -> None:
    audio = load_pcm()
    gemma = json.loads(GEMMA_SIGNALS.read_text())
    result = {
        "clip": "kor.mp4 (38.2s, 한국어 자기소개)",
        "gemma_baseline": {
            "model": "gemma-4-E4B vLLM (QA 스택 실턴, GPU)",
            "windowed_3s_texts": gemma["windowTranscripts"],
            "transcript_full": gemma["transcriptFull"],
        },
        "whisper": [],
    }
    for name in MODELS:
        print(f"[bench] {name} ...", flush=True)
        try:
            result["whisper"].append(bench_model(name, audio))
        except Exception as exc:  # 모델 별칭 미지원 등 — 후보 하나 실패가 비교 전체를 죽이지 않게
            print(f"[bench] {name} FAILED: {exc}", flush=True)
            result["whisper"].append({"model": name, "error": str(exc)})
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"saved -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
