#!/usr/bin/env python3
"""문장 정렬 피드백 프로브 — "이 문장 말할 때 망설였다/절었다"가 현 재료로 가능한지 검증.

설계 검증 대상(스키마 v2 후보): 슬라이딩 레인(실시간)은 유지하고, turn_end에 문장 단위
조립 레이어를 얹는다. 이 프로브는 오프라인으로 그 조립만 흉내 낸다:
  1. 문장끝 감지 = 텍스트 1차(종결 문장부호) — 3s 윈도우 전사를 글자-시간 보간으로 시간축에 매핑
  2. 문장별 PCM 재절단 → ProsodyExtractor(프로덕션 함수) — 속도/억양/쉼/에너지
  3. 문장 전 쉼(pause_before) = 위치 있는 silence run(프로소디 레인과 같은 정의)으로 망설임 측정
  4. 3s nv read를 시간 겹침으로 문장에 매핑

사용: .venv/bin/python sentence_probe.py <signals.json> <audio16k.wav> [-o out.json]
한계(의도): 시간은 보간 근사(±1s급) — 프로덕션은 휴지 위치 스냅·단어 타임스탬프로 정밀화.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from giljobe.analysis.prosody import ProsodyExtractor  # noqa: E402

from make_turn_handoff import agg_visual  # noqa: E402 - 턴 집계와 동일 규칙 재사용(드리프트 0)

SENT_END = re.compile(r"(?<=[.?!])\s+")
HANGUL = re.compile(r"[가-힣]")  # 한국어 음절수 = 한글 글자수(텍스트 기반 정밀 속도)


def char_timeline(windows: list[dict]) -> tuple[str, list[float]]:
    """3s 윈도우 전사들을 이어붙이고 글자마다 시간을 보간 부여(윈도우 내 균등 분포 가정).
    윈도우 사이 결합 공백은 앞 윈도우 끝 시각을 갖는다."""
    text_parts: list[str] = []
    times: list[float] = []
    for w in windows:
        txt = w["transcript"].strip()
        if not txt:
            continue
        start = w["t"] - w["window_s"] / 2
        if text_parts:  # 결합 공백
            text_parts.append(" ")
            times.append(start)
        text_parts.append(txt)
        times.extend(start + (i / len(txt)) * w["window_s"] for i in range(len(txt)))
    return "".join(text_parts), times


def split_sentences(text: str, times: list[float]) -> list[dict]:
    """종결 문장부호로 분할 — 각 문장의 [t_start, t_end] 추정(글자 보간)."""
    out: list[dict] = []
    pos = 0
    for m in SENT_END.finditer(text + " "):
        seg = text[pos:m.start()].strip()
        if seg:
            out.append({
                "text": seg,
                "t": [round(times[pos], 1), round(times[min(m.start() - 1, len(times) - 1)], 1)],
            })
        pos = m.end()
    return out


def positioned_silence_runs(samples: np.ndarray, sr: int) -> list[tuple[float, float]]:
    """프로소디 레인과 같은 정의(99분위-18dB, 25ms/10ms RMS)로 (시작초, 길이초) 휴지 목록.
    레인의 _silence_runs는 길이만 반환 — 문장 전 쉼(망설임)엔 위치가 필요해 여기서 확장."""
    frame, hop = 400, 160
    n = 1 + max(0, (len(samples) - frame) // hop)
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(samples[idx] ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-10) / max(float(rms.max()), 1e-8))
    threshold = float(np.percentile(db, 99)) - 18.0
    silent = db < threshold
    runs: list[tuple[float, float]] = []
    start = None
    for i, s in enumerate(silent):
        if s and start is None:
            start = i
        elif not s and start is not None:
            dur = (i - start) * hop / sr
            if dur >= 0.1:
                runs.append((start * hop / sr, dur))
            start = None
    return runs


def pause_before(t_start: float, runs: list[tuple[float, float]]) -> float | None:
    """문장 시작 직전(±0.8s 탐색창) 가장 긴 휴지 — '이 문장 꺼내기 전에 망설였나'."""
    near = [d for (s, d) in runs if t_start - 0.8 <= s + d <= t_start + 0.8]
    return round(max(near), 2) if near else None


def _overlapping(t0: float, t1: float, windows: list[dict]) -> list[dict]:
    """문장 [t0,t1]과 시간이 겹치는 3s 윈도우 레코드(겹침 단위=3s — 프로브 근사.
    프로덕션은 grounder.window_metrics(t0,t1)로 정확 구간 집계가 같은 비용)."""
    return [w for w in windows if t0 < w["t"] + 1.5 and w["t"] - 1.5 < t1]


def _speech_row(s: dict, samples: np.ndarray, sr: int, ex: ProsodyExtractor,
                runs: list[tuple[float, float]]) -> dict:
    """문장 구간 PCM 재절단 → 프로소디 전항목(turn_handoff speech와 같은 재료)."""
    t0, t1 = s["t"]
    dur = t1 - t0
    syl = len(HANGUL.findall(s["text"]))
    out: dict = {
        "pause_before_s": pause_before(t0, runs),                          # 망설임
        "rate_syl_per_s": round(syl / dur, 1) if dur > 0.3 else None,      # 텍스트 기반(한글=음절)
    }
    pcm = samples[int(t0 * sr):int(t1 * sr)]
    m = ex.extract((pcm * 32768).astype(np.int16).tobytes(), sample_rate=sr) if dur >= 1.0 else None
    if not m:
        out["note"] = "1초 미만/분석 불가 — 프로소디 생략"
        return out
    out.update({
        "articulation_rate_syl_per_s": m["rate"].get("articulation_rate_syl_per_s"),
        "pitch_sd_semitone": m["pitch"].get("sd_semitone"),                # 억양(2 미만=단조)
        "pdq": m["pitch"].get("pdq"),
        "voiced_ratio": m["pitch"].get("voiced_ratio"),
        "junctures_within": (m["pauses"]["pause_count_ge_0p25"]            # 문장 내 절음
                             + m["pauses"]["short_juncture_count_0p1_0p25"]),
        "longest_pause_s": (m["pauses"]["longest_pauses_s"] or [None])[0],
        "energy_end_delta_db": m["energy"].get("half_to_half_delta_db"),   # 말끝 흐림(음수=감소)
    })
    return out


def _nonverbal_row(overlap: list[dict]) -> dict | None:
    """겹치는 3s nv read 요약 — 상태·강도·note(모델 관찰)."""
    nvs = [w["nonverbal"] for w in overlap if w.get("nonverbal")]
    if not nvs:
        return None
    return {
        "states": sorted({n["state"] for n in nvs}),
        "intensity_mean": round(sum(n.get("intensity", 0) for n in nvs) / len(nvs), 2),
        "notes": [n["note"] for n in nvs if n.get("note")],
    }


def sentence_rows(sents: list[dict], samples: np.ndarray, sr: int,
                  runs: list[tuple[float, float]], nv_windows: list[dict]) -> list[dict]:
    """문장별 행 = turn_handoff와 같은 3섹션(speech/visual/nonverbal) — 스키마 패밀리 일관."""
    ex = ProsodyExtractor()
    rows: list[dict] = []
    for s in sents:
        t0, t1 = s["t"]
        overlap = _overlapping(t0, t1, nv_windows)
        rows.append({
            "text": s["text"],
            "t": s["t"],
            "dur_s": round(t1 - t0, 1),
            "speech": _speech_row(s, samples, sr, ex, runs),
            "visual": agg_visual([w.get("objective_nonverbal") for w in overlap]),
            "nonverbal": _nonverbal_row(overlap),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("signals_json", type=Path)
    ap.add_argument("wav", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()
    payload = json.loads(args.signals_json.read_text())
    windows = sorted((r for r in payload["records"] if r["type"] == "window"), key=lambda w: w["t"])
    with wave.open(str(args.wav)) as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1, "16k mono 필요"
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16) / 32768.0
    text, times = char_timeline(windows)
    sents = split_sentences(text, times)
    runs = positioned_silence_runs(samples, 16000)
    rows = sentence_rows(sents, samples, 16000, runs, windows)
    result = {"type": "sentence_table_probe", "sentences": rows,
              "meta": {"n_sentences": len(rows), "silence_runs": len(runs),
                       "caveat": "시간=글자 보간 근사(±1s급) — 프로덕션은 휴지 스냅/단어 타임스탬프"}}
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(out + "\n")
        print(f"wrote {args.out} ({len(rows)} sentences)")
    else:
        print(out)


if __name__ == "__main__":
    main()
