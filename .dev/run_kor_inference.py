"""kor.mp4 풀 추론 덤프 (.dev 스크래치) — 젬마(E4B)의 비언어 비평·전사·심층비평을
윈도우별로 raw 모델 출력 + 파싱 결과 + 지연까지 전부 찍는다. 나/클로드/젬마 괴리 비교용.

재현: vLLM 기동 후 `PYTHONPATH=src .venv/bin/python .dev/run_kor_inference.py`
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from giljobe.analysis.critic import (
    EVAL_SYSTEM,
    NONVERBAL_SYSTEM,
    _audio_part,
    _extract_json,
    _video_part,
)
from giljobe.analysis.transcriber import GemmaNativeTranscriber
from giljobe.llm.vllm_client import default_vllm_client
from giljobe.media.window_assembly import assemble_audio_url, assemble_video_url

CLIP = Path(__file__).parent.parent / "kor.mp4"
SR = 16000
OUT = Path(__file__).parent / "kor_gemma_out.json"


def pcm(ss: float, dur: float) -> bytes:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(CLIP),
           "-vn", "-ar", str(SR), "-ac", "1", "-f", "s16le", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=30).stdout


def frames(ss: float, dur: float, fps: float = 1.0) -> list[bytes]:
    with tempfile.TemporaryDirectory() as d:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(CLIP),
               "-vf", f"fps={fps:g}", "-q:v", "3", str(Path(d, "f%05d.jpg"))]
        subprocess.run(cmd, capture_output=True, timeout=30)
        return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"))]


client = default_vllm_client()
client.health()
transcriber = GemmaNativeTranscriber(client=client)
record: dict = {"clip": CLIP.name, "model": "google/gemma-4-E4B-it", "nv_grid": [], "eval": [], "full_transcript_chunks": []}


def raw_chat(system: str, content: list[dict], max_tokens: int):
    resp = client.chat({
        "model": "google/gemma-4-E4B-it",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "max_tokens": max_tokens, "temperature": 0.0,
    })
    return resp["choices"][0]["message"]["content"], resp.get("usage", {})


print("=" * 78)
print("PART 1 — 비언어 read + 전사 (3초 그리드, 라이브 파이프라인과 동일)")
print("=" * 78)
for ss in range(0, 37, 3):
    fr = frames(ss, 3)
    pc = pcm(ss, 3)
    content = [_video_part(assemble_video_url(fr))]
    if pc:
        content.append(_audio_part(assemble_audio_url(pc, sample_rate=SR)))
    content.append({"type": "text", "text": "Read the candidate's non-verbal state now."})
    t0 = time.perf_counter(); raw_nv, usage_nv = raw_chat(NONVERBAL_SYSTEM, content, 96); nv_lat = time.perf_counter() - t0
    try:
        parsed_nv = _extract_json(raw_nv)
    except Exception as e:  # noqa: BLE001
        parsed_nv = {"_parse_error": str(e)}
    t0 = time.perf_counter(); tx = transcriber.transcribe(pc, sample_rate=SR); tx_lat = time.perf_counter() - t0
    row = {"ss": ss, "dur": 3, "frames": len(fr),
           "nv_raw": raw_nv, "nv_parsed": parsed_nv, "nv_latency_s": round(nv_lat, 3),
           "transcript": tx, "stt_latency_s": round(tx_lat, 3)}
    record["nv_grid"].append(row)
    print(f"\n[{ss:>2}-{ss+3:>2}s] (frames={len(fr)})")
    print(f"  ▶ 비언어 RAW   : {raw_nv!r}")
    print(f"  ▶ 비언어 파싱  : state={parsed_nv.get('state')} intensity={parsed_nv.get('intensity')} "
          f"note={parsed_nv.get('note')!r}  ({nv_lat:.2f}s)")
    print(f"  ▶ 전사         : {tx!r}  ({tx_lat:.2f}s)")


print("\n" + "=" * 78)
print("PART 2 — 심층 비평 evaluate (16초 윈도우, 채널② 풀 비평)")
print("=" * 78)
for ss, dur in [(0, 16), (16, 16), (21, 16)]:
    fr = frames(ss, dur)
    pc = pcm(ss, dur)
    content = [_video_part(assemble_video_url(fr)), _audio_part(assemble_audio_url(pc, sample_rate=SR)),
               {"type": "text", "text": "Evaluate this answer window."}]
    t0 = time.perf_counter(); raw_ev, usage_ev = raw_chat(EVAL_SYSTEM, content, 640); ev_lat = time.perf_counter() - t0
    try:
        parsed_ev = _extract_json(raw_ev)
    except Exception as e:  # noqa: BLE001
        parsed_ev = {"_parse_error": str(e)}
    record["eval"].append({"ss": ss, "dur": dur, "raw": raw_ev, "parsed": parsed_ev, "latency_s": round(ev_lat, 3)})
    print(f"\n[{ss}-{ss+dur}s] ({ev_lat:.2f}s, frames={len(fr)})")
    print(json.dumps(parsed_ev, ensure_ascii=False, indent=2))


print("\n" + "=" * 78)
print("PART 3 — 전체 전사 (긴 청크로 읽기 좋게)")
print("=" * 78)
chunks = []
for ss, dur in [(0, 12), (12, 12), (24, 14)]:
    pc = pcm(ss, dur)
    t0 = time.perf_counter(); tx = transcriber.transcribe(pc, sample_rate=SR); lat = time.perf_counter() - t0
    chunks.append(tx)
    record["full_transcript_chunks"].append({"ss": ss, "dur": dur, "transcript": tx, "latency_s": round(lat, 3)})
    print(f"\n[{ss}-{ss+dur}s] ({lat:.2f}s) {tx!r}")
print("\n--- 청크 이어붙인 전체 전사 ---")
print(" ".join(chunks))
print("\n--- 3초 그리드 이어붙인 전체 전사 ---")
print(" ".join(r["transcript"] for r in record["nv_grid"]))

OUT.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n저장: {OUT}")
