"""★ Slice 2 게이팅 스파이크 — gemma-4-E4B가 한국어를 충분히 전사하는가?

목적: E4B가 네이티브 AV라 같은 모델로 전사를 시켜보고, 충분하면 별도 STT 엔진
(faster-whisper)을 안 붙인다. GT가 없어 자동 WER은 불가 → 이 테스트는 **측정치를
`tests/evidence/spike-transcribe.json`에 남기고**, 기계로 확인 가능한 바닥선만 assert한다
(빈출력 아님 + 한글 포함 = 영어 번역/거부가 아님). verbatim·환각·지연수용은 사람 루브릭.

실 vLLM 의존 → `client.health()` 게이트로 다운이면 skip(gje `test_e2e_pipeline` 관례).
재현: vLLM 기동 후 `.venv/bin/python -m pytest tests/test_spike_transcribe.py -v -s`
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from giljobe.analysis.transcriber import GemmaNativeTranscriber
from giljobe.llm.vllm_client import default_vllm_client

# 실측 한국어 클립(테스트 픽스처 — src로 import하지 않고 repo 루트 경로만 참조).
# 사용자 제공: SK하이닉스 지원 면접 자기소개(약 38s, 0s부터 발화 연속).
CLIP = Path(__file__).parent.parent / "kor.mp4"

# 발화 전체에 걸친 대표 윈도우로 과대일반화 방지: 라이브 nv 그리드와 같은 3s 네 개
# (도입/중반/기술/마무리) + 맥락 6s 한 개(더 주면 나아지는지).
WINDOWS = [(0.0, 3.0), (10.0, 3.0), (20.0, 3.0), (30.0, 3.0), (10.0, 6.0)]

SAMPLE_RATE = 16000
# 기계 측정치(재생성 가능)는 .raw.json에. 사람 루브릭 판정·결정은 옆의 큐레이션 정본
# spike-transcribe.json에 둔다(gje .sisyphus 관례 — 측정치/판정 분리, 재실행이 판정을 안 덮게).
EVIDENCE = Path(__file__).parent / "evidence" / "spike-transcribe.raw.json"


def _extract_pcm(clip: Path, *, ss: float, dur: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """클립의 [ss, ss+dur) 구간을 16k mono Int16 PCM 바이트로 추출(라이브 경로와 동일 리샘플)."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(clip),
        "-vn", "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg pcm extract failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def test_spike_transcribe():
    client = default_vllm_client()
    try:
        client.health()
    except Exception as e:  # noqa: BLE001 - 다운이면 스파이크 불가 → skip
        pytest.skip(f"vLLM down ({client.base_url}): {e}")

    if not CLIP.exists():
        pytest.skip(f"clip missing: {CLIP}")

    transcriber = GemmaNativeTranscriber(client=client)
    results: list[dict] = []
    for ss, dur in WINDOWS:
        pcm = _extract_pcm(CLIP, ss=ss, dur=dur)
        t0 = time.perf_counter()
        text = transcriber.transcribe(pcm, sample_rate=SAMPLE_RATE)
        latency = time.perf_counter() - t0
        row = {
            "clip": CLIP.name,
            "ss": ss,
            "dur": dur,
            "pcm_bytes": len(pcm),
            "latency_s": round(latency, 3),
            "chars": len(text),
            "hangul": _has_hangul(text),
            "transcript": text,
        }
        results.append(row)
        print(f"\n[{CLIP.name} {ss:g}-{ss + dur:g}s | {latency:.2f}s | {len(text)}자] {text!r}")

    # 측정치는 assert 이전에 먼저 기록(루브릭 판정용; 실패해도 증거 보존). 판정/결정은
    # 정본 spike-transcribe.json에 별도 큐레이션(여기선 raw 측정치만).
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "slice": 2,
                "spike": "stt-necessity",
                "model": transcriber.model,
                "sample_rate": SAMPLE_RATE,
                "clip": CLIP.name,
                "windows": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 기계 확인 바닥선: 비어있지 않은 음성 윈도우는 한글 전사를 내야 한다(영어 번역/거부 아님).
    for r in results:
        assert r["chars"] > 0, f"empty transcript: {r['clip']} {r['ss']}s"
        assert r["hangul"], f"no Hangul (translation/refusal?): {r['clip']} {r['ss']}s → {r['transcript']!r}"
