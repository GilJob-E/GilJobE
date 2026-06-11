#!/usr/bin/env python3
"""동적(문장) 윈도우 경계 검출 리플레이 시뮬레이션 — 채택 결정용 수치 산출.

실측 데이터(window 전사 + wav 휴지 위치)를 시간순으로 재생하며 경계 검출기를 돌린다:
  경계 = 텍스트 1차(종결 문장부호) + 쉼 스냅 2차(±0.7s) + 타임아웃 백스톱(기본 10s)
측정: ① 경계 확정 지연(문장 실제 끝 → 검출기 확정) ② nv 호출 수(3s 그리드 대비)
     ③ 타임아웃 발동률 ④ 세그먼트 길이 분포.
STT 미확정 대응: 청크 크기를 파라미터로 재청킹해(글자-시간 보간) 3s(현행 gemma)와
1s(스트리밍급)를 비교 — 경계 지연의 지배 변수가 STT 청크임을 정량화한다.

사용: .venv/bin/python boundary_sim.py <signals.json> <audio16k.wav> [-o out.json]
"""
from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

from sentence_probe import SENT_END, char_timeline, positioned_silence_runs

STT_LAT = 0.2      # 청크 종료→텍스트 도착(실측 stt 0.19s)
SNAP_S = 0.7       # 보간 경계 → 가장 가까운 휴지 시작으로 스냅 허용폭
TIMEOUT_S = 10.0   # 경계 없이 이만큼 지나면 강제 경계(실시간성 백스톱)
NV_FPS_CAP = (2.0, 12)  # 문장 nv 호출 프레임: 2fps, 최대 12장


def rechunk(times: list[float], chunk_s: float) -> list[dict]:
    """글자-시간 보간으로 임의 청크 그리드 재구성 — STT 청크 크기를 시뮬 레버로 만든다."""
    chunks: list[dict] = []
    if not times:
        return chunks
    k = 0
    while k * chunk_s < times[-1] + chunk_s:
        lo = next((i for i, t in enumerate(times) if t >= k * chunk_s), None)
        hi = next((i for i, t in enumerate(times) if t >= (k + 1) * chunk_s), len(times))
        if lo is not None and hi > lo:
            chunks.append({"end_t": (k + 1) * chunk_s, "char_hi": hi})
        k += 1
    return chunks


def snap(t_raw: float, runs: list[tuple[float, float]]) -> tuple[float, bool]:
    """보간 경계를 가장 가까운 휴지 시작점으로 스냅(±SNAP_S) — 음향이 시간 정밀도를 복원."""
    if not runs:
        return t_raw, False
    s = min((r[0] for r in runs), key=lambda x: abs(x - t_raw))
    return (s, True) if abs(s - t_raw) <= SNAP_S else (t_raw, False)


def replay(text: str, times: list[float], runs: list[tuple[float, float]],
           chunk_s: float) -> list[dict]:
    """청크 도착 순 리플레이 — 도착할 때마다 새 문장부호 경계 스캔, 없으면 타임아웃 체크."""
    segs: list[dict] = []
    consumed = 0          # 경계 처리 완료된 글자 위치
    seg_start = 0.0
    for ch in rechunk(times, chunk_s):
        arrive = ch["end_t"] + STT_LAT
        found = False
        for m in SENT_END.finditer(text[:ch["char_hi"]] + " "):
            if m.start() <= consumed or m.start() >= ch["char_hi"]:
                continue
            t_raw = times[min(m.start() - 1, len(times) - 1)]
            t_b, snapped = snap(t_raw, runs)
            segs.append({
                "t": [round(seg_start, 1), round(t_b, 1)],
                "text": text[consumed:m.start()].strip()[:60],
                "source": "punct", "snapped": snapped,
                "confirm_lag_s": round(arrive - t_b, 2),
            })
            seg_start, consumed, found = t_b, m.end(), True
        if not found and ch["end_t"] - seg_start > TIMEOUT_S:
            t_b, snapped = snap(ch["end_t"], runs)
            segs.append({
                "t": [round(seg_start, 1), round(t_b, 1)],
                "text": text[consumed:ch["char_hi"]].strip()[:60] + "…",
                "source": "timeout", "snapped": snapped,
                "confirm_lag_s": round(arrive - t_b, 2),
            })
            seg_start = t_b
            consumed = ch["char_hi"]
    if consumed < len(text.strip()):  # 턴 종료 flush(잔여)
        segs.append({"t": [round(seg_start, 1), round(times[-1], 1)],
                     "text": text[consumed:].strip()[:60],
                     "source": "turn_end", "snapped": False, "confirm_lag_s": None})
    return segs


def stats(segs: list[dict], total_s: float) -> dict:
    lags = [s["confirm_lag_s"] for s in segs if s["confirm_lag_s"] is not None]
    durs = [s["t"][1] - s["t"][0] for s in segs]
    frames = [min(NV_FPS_CAP[1], max(1, round(d * NV_FPS_CAP[0]))) for d in durs]
    return {
        "segments": len(segs),
        "timeout_forced": sum(1 for s in segs if s["source"] == "timeout"),
        "snapped_ratio": round(sum(1 for s in segs if s["snapped"]) / len(segs), 2) if segs else None,
        "confirm_lag_median_s": round(float(np.median(lags)), 2) if lags else None,
        "confirm_lag_p95_s": round(float(np.percentile(lags, 95)), 2) if lags else None,
        "seg_dur_median_s": round(float(np.median(durs)), 1) if durs else None,
        "seg_dur_max_s": round(max(durs), 1) if durs else None,
        "nv_calls_dynamic": len(segs),
        "nv_calls_grid_3s": int(np.ceil(total_s / 3)),
        "nv_frames_per_call_mean": round(float(np.mean(frames)), 1) if frames else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("signals_json", type=Path)
    ap.add_argument("wav", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()
    payload = json.loads(args.signals_json.read_text())
    windows = sorted((r for r in payload["records"] if r["type"] == "window"), key=lambda w: w["t"])
    with wave.open(str(args.wav)) as wf:
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16) / 32768.0
        total_s = wf.getnframes() / wf.getframerate()
    text, times = char_timeline(windows)
    runs = positioned_silence_runs(samples, 16000)
    result: dict = {"type": "boundary_sim", "clip": args.wav.name, "duration_s": round(total_s, 1)}
    for chunk_s in (3.0, 1.0):
        segs = replay(text, times, runs, chunk_s)
        result[f"chunk_{chunk_s}s"] = {"stats": stats(segs, total_s), "segments": segs}
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(out + "\n")
        print(f"wrote {args.out}")
    for chunk_s in (3.0, 1.0):
        print(f"[{args.wav.name} | chunk {chunk_s}s]", json.dumps(result[f"chunk_{chunk_s}s"]["stats"], ensure_ascii=False))


if __name__ == "__main__":
    main()
