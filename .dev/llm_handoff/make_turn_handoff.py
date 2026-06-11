#!/usr/bin/env python3
"""턴 핸드오프 스키마 프로토타입 — /analysis/signals 레코드를 턴 단위 `turn_handoff` JSON으로 집계 (대개편 #2).

현재 next-question LLM에는 turn_end.transcript_full(2,000자 절단)만 간다. 이 스크립트는
"수치+분석 전달이 원설계"를 채우는 스키마 후보를 실데이터(qa/*.json)로 뽑아 검증한다:
  - speech/visual = eval 윈도우들의 objective_vocal/objective_visual을 **per-window와 같은 shape**로
    집계 → 프로덕션 describe_vocal/describe_visual을 그대로 재사용해 prompt_block을 렌더(드리프트 0).
  - 집계 원칙: 카운트=합산, 비율=합산 후 재계산(평균-의-비율 함정 회피), 분포 통계=가중평균.
  - analysis = eval 비평 텍스트를 시간 참조 보존한 채 수집(집계 LLM 호출 없음 — 1순위 레이턴시).
  - meta.flags = 비평 텍스트 vs 실측 모순 자동 플래그 맛보기(예: "단조" 주장 vs sd_semitone≥2).

사용: .venv/bin/python make_turn_handoff.py <signals.json> [-o out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from giljobe.analysis.grounding import describe_visual  # noqa: E402
from giljobe.analysis.prosody import describe_vocal  # noqa: E402

SCHEMA_VERSION = 1
MAX_CONCERNS = 9       # 머신 섹션 상한(eval 3윈도우 × 3개)
PROMPT_CONCERNS = 6    # prompt_block에 들어가는 우려 상한(토큰 예산)


def _wmean(pairs: list[tuple[float | None, float]], nd: int) -> float | None:
    """가중평균 — 값 None/가중치 0은 건너뜀(관측 없던 윈도우가 평균을 끌어내리지 않게)."""
    valid = [(v, w) for v, w in pairs if v is not None and w]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    return round(sum(v * w for v, w in valid) / total_w, nd)


# ── speech: objective_vocal 집계 ──────────────────────────────────────────────

def _agg_pitch(ms: list[dict]) -> dict:
    """유성 프레임 가중평균. 풀링 SD가 아닌 '윈도우 내 변동의 평균'을 쓴다 — 풀링하면
    윈도우 간 선언적 하강(declination)까지 섞여 억양 생동감이 인위 팽창한다."""
    valid = [(m["pitch"], m["duration_s"]) for m in ms if "sd_semitone" in m.get("pitch", {})]
    if not valid:
        return {"note": "유성 구간 부족(피치 측정 불가)"}
    out: dict = {"voiced_frames": sum(p["voiced_frames"] for p, _ in valid)}
    out["voiced_ratio"] = _wmean([(p.get("voiced_ratio"), d) for p, d in valid], 3)
    frames = [(p, p["voiced_frames"]) for p, _ in valid]
    for key, nd in [("mean_hz", 1), ("sd_semitone", 2), ("pdq", 4), ("range_st_10_90", 2),
                    ("detrended_sd_semitone", 2), ("octave_error_frac", 3)]:
        out[key] = _wmean([(p.get(key), w) for p, w in frames], nd)
    return out


def _agg_rate(ms: list[dict], dur: float) -> dict:
    """비율은 평균 내지 않고 합산 후 재계산 — 짧은 꼬리 윈도우가 같은 가중을 받는 함정 회피.
    phonation 시간은 nuclei/articulation_rate로 역산해 풀링한다."""
    nuclei = sum(m.get("rate", {}).get("syllable_nuclei", 0) for m in ms)
    phonation = 0.0
    for m in ms:
        r = m.get("rate", {})
        artic = r.get("articulation_rate_syl_per_s") or 0
        if artic > 0:
            phonation += r.get("syllable_nuclei", 0) / artic
    return {
        "syllable_nuclei": nuclei,
        "speech_rate_syl_per_s": round(nuclei / dur, 2) if dur else None,
        "articulation_rate_syl_per_s": round(nuclei / phonation, 2) if phonation else None,
    }


def _agg_pauses(ms: list[dict]) -> dict:
    """카운트 합산 + 최장 쉼은 전 윈도우 병합 top5. 중앙값은 정확 풀링 불가 → run 수 가중 근사."""
    pall = [m.get("pauses", {}) for m in ms]
    longest = sorted((x for p in pall for x in p.get("longest_pauses_s", [])), reverse=True)[:5]
    med_w = [(p.get("juncture_median_s"),
              p.get("pause_count_ge_0p25", 0) + p.get("short_juncture_count_0p1_0p25", 0))
             for p in pall]
    return {
        "pause_count_ge_0p25": sum(p.get("pause_count_ge_0p25", 0) for p in pall),
        "short_juncture_count_0p1_0p25": sum(p.get("short_juncture_count_0p1_0p25", 0) for p in pall),
        "juncture_median_s": _wmean(med_w, 3),
        "longest_pauses_s": longest,
    }


def _agg_energy(ms: list[dict]) -> dict:
    """dB가 윈도우 상대값(피크=0)이라 절대 비교 불가 — 윈도우 내 추세만 길이 가중평균.
    trough는 전 윈도우 최솟값(가장 깊었던 dip)."""
    valid = [(m["energy"], m["duration_s"]) for m in ms if "half_to_half_delta_db" in m.get("energy", {})]
    if not valid:
        return {"note": "측정 불가"}
    troughs = [e["sounding_trough_db"] for e, _ in valid if e.get("sounding_trough_db") is not None]
    return {
        "rel_db_slope_per_s": _wmean([(e.get("rel_db_slope_per_s"), d) for e, d in valid], 3),
        "half_to_half_delta_db": _wmean([(e.get("half_to_half_delta_db"), d) for e, d in valid], 2),
        "sounding_trough_db": round(min(troughs), 2) if troughs else None,
    }


def agg_vocal(metrics: list[dict]) -> dict | None:
    ms = [m for m in metrics if m]
    if not ms:
        return None
    dur = sum(m.get("duration_s", 0) for m in ms)
    return {
        "windows": len(ms),
        "duration_s": round(dur, 2),
        "pitch": _agg_pitch(ms),
        "rate": _agg_rate(ms, dur),
        "pauses": _agg_pauses(ms),
        "energy": _agg_energy(ms),
    }


# ── visual: objective_visual 집계 ─────────────────────────────────────────────

FACE_KEYS = [("smile_mean", 3), ("smile_ratio", 2), ("brow_down_mean", 3), ("mouth_press_mean", 3),
             ("gaze_off_mean", 3), ("yaw_std_deg", 2), ("pitch_std_deg", 2), ("lip_motion", 5)]
POSE_KEYS = [("posture_std", 4), ("head_sway", 4), ("wrist_visible", 2), ("wrist_motion", 4)]


def _agg_lane(ms: list[dict], frames_key: str, ratio_key: str, keys: list[tuple[str, int]],
              sums: list[str], maxes: list[str]) -> dict:
    """face/pose 공통 집계 — 검출 프레임 수(frames×seen_ratio)를 가중치로, 미검출 윈도우는
    coverage에만 반영하고 특징 평균에선 제외한다(0이 평균을 오염시키지 않게)."""
    total = sum(m.get(frames_key, 0) for m in ms)
    det = [(m, m.get(frames_key, 0) * m.get(ratio_key, 0)) for m in ms]
    det_n = sum(w for _, w in det)
    out = {frames_key: total, ratio_key: round(det_n / total, 2) if total else 0.0}
    if not det_n:
        return out
    for key, nd in keys:
        out[key] = _wmean([(m.get(key), w) for m, w in det], nd)
    for key in sums:
        out[key] = sum(m.get(key, 0) for m, w in det if w)
    for key in maxes:
        out[key] = max((m.get(key, 0) for m, w in det if w), default=0)
    return out


def agg_visual(metrics: list[dict]) -> dict | None:
    ms = [m for m in metrics if m]
    if not ms:
        return None
    out = {"windows": len(ms)}
    out.update(_agg_lane(ms, "face_frames", "face_seen_ratio", FACE_KEYS,
                         sums=["blink_count"], maxes=["smile_max"]))
    out.update(_agg_lane(ms, "pose_frames", "pose_seen_ratio", POSE_KEYS,
                         sums=["gesture_events"], maxes=[]))
    return out


# ── nonverbal(3s window nv) + analysis(eval 텍스트) ──────────────────────────

def agg_nonverbal(windows: list[dict]) -> dict:
    """3s nv read의 상태 분포 — 객관 수치는 visual 집계가 정본이므로 여기선 모델 read만 요약."""
    nvs = [w["nonverbal"] for w in windows if w.get("nonverbal")]
    if not nvs:
        return {"windows": 0}
    states = Counter(nv["state"] for nv in nvs)
    return {
        "windows": len(nvs),
        "states": {s: round(c / len(nvs), 2) for s, c in states.most_common()},
        "intensity_mean": round(sum(nv.get("intensity", 0) for nv in nvs) / len(nvs), 2),
        "notes": [nv["note"] for nv in nvs if nv.get("note")][-3:],  # 최근 3개(턴 후반 상태)
    }


def collect_analysis(evals: list[dict]) -> dict:
    """eval LLM 텍스트를 시간 참조 보존한 채 수집. vocal/visual 텍스트 축은 의도적으로 버린다 —
    수치-번역이거나 수치와 모순(meta.flags)이므로 objective 집계가 정본."""
    concerns, highlights, verbal = [], [], []
    for ev in evals:
        win = [ev["window_start_s"], round(ev["window_start_s"] + ev["window_dur_s"], 1)]
        concerns += [{"window": win, "text": c} for c in ev.get("critique", [])]
        highlights += [{"window": win, "text": k} for k in ev.get("key_observations", [])]
        if ev.get("verbal"):
            verbal.append({"window": win, **ev["verbal"]})
    return {"concerns": concerns[:MAX_CONCERNS], "highlights": highlights[:MAX_CONCERNS], "verbal": verbal}


def flag_conflicts(evals: list[dict]) -> list[dict]:
    """비평 텍스트 vs 실측 모순 자동 플래그(맛보기) — '단조' 주장인데 sd_semitone≥2(참조: 2 미만=단조)."""
    flags = []
    for ev in evals:
        sd = (ev.get("objective_vocal") or {}).get("pitch", {}).get("sd_semitone")
        if sd is None or sd < 2.0:
            continue
        texts = list((ev.get("vocal") or {}).values()) + ev.get("critique", [])
        for t in texts:
            # 부정문("단조롭지 않-")은 측정과 일치하는 주장이므로 제외
            if "단조" in t and "단조롭지 않" not in t and "단조하지 않" not in t:
                flags.append({
                    "window": [ev["window_start_s"], round(ev["window_start_s"] + ev["window_dur_s"], 1)],
                    "claim": t[:60], "measured_sd_semitone": sd,
                    "verdict": "측정과 모순(sd_semitone≥2면 단조 아님)",
                })
                break  # 윈도우당 1건이면 충분
    return flags


# ── prompt_block 렌더 + 조립 ─────────────────────────────────────────────────

def render_prompt_block(handoff: dict, *, flags: list[dict]) -> list[str]:
    """ai-engine이 '\\n'.join으로 끼울 한국어 블록(전사 제외 — lastAnswer는 기존 경로 유지).
    JSON에는 **줄 배열**로 싣는다 — \\n 이스케이프 한 줄 덩어리는 QA에서 사람이 못 읽는다.
    빈 문자열 = 섹션 구분. describe_vocal/visual은 프로덕션 함수 재사용(참조범위·차단 문구 포함)."""
    lines: list[str] = []

    def section(header: str, body: list[str]) -> None:
        if lines:
            lines.append("")  # 섹션 사이 빈 줄
        lines.append(header)
        lines.extend(body)

    sp = handoff.get("speech")
    if sp:
        section(f"[음성 전달 실측치 — 발화 {sp['duration_s']}초, {sp['windows']}개 구간 집계]",
                describe_vocal(sp).split("\n"))
    vis = handoff.get("visual")
    if vis and (desc := describe_visual(vis)):
        section("[시각 실측치 — MediaPipe]", desc.split("\n"))
    nv = handoff.get("nonverbal", {})
    if nv.get("windows"):
        states = ", ".join(f"{s} {int(r * 100)}%" for s, r in nv["states"].items())
        section(f"[비언어 상태 분포 — 3초 단위 read {nv['windows']}개]",
                [f"- 상태: {states} · 평균 강도 {nv['intensity_mean']}"])
    concerns = handoff.get("analysis", {}).get("concerns", [])[:PROMPT_CONCERNS]
    if concerns:
        section("[면접관 비평 — 구간별 핵심 우려]",
                [f"- ({int(c['window'][0])}~{int(c['window'][1])}초) {c['text']}" for c in concerns])
    # 비평이 실측과 모순되면(예: '단조' 주장 vs sd≥2) 수치가 정본임을 소비자 LLM에 명시 —
    # eval 프롬프트 간소화(vocal/visual 텍스트 축 제거)로 근본 해소되기 전까지의 방어선.
    if flags:
        section("[실측 우선 규칙]",
                ["위 비평 중 억양·음량·속도 등 전달력 묘사가 실측치와 다르면 실측치를 따를 것"
                 "(예: F0 표준편차 2 semitone 미만일 때만 '단조')."])
    return lines


def build_turn_handoff(payload: dict) -> dict:
    records = payload.get("records", payload if isinstance(payload, list) else [])
    evals = [r["eval"] for r in records if r.get("type") == "eval"]
    windows = [r for r in records if r.get("type") == "window"]
    turn_ends = [r for r in records if r.get("type") == "turn_end"]
    latest_te = turn_ends[-1] if turn_ends else {}
    transcript = latest_te.get("transcript_full") or " ".join(
        w["transcript"] for w in windows if w.get("transcript", "").strip())
    handoff = {
        "type": "turn_handoff",
        "schema_version": SCHEMA_VERSION,
        "session_id": payload.get("sessionId") or (records[0].get("session_id") if records else ""),
        "t": latest_te.get("t"),
        "transcript": transcript,  # 무절단 — 2,000자 절단은 소비자 정책이지 스키마가 아님
        "speech": agg_vocal([e.get("objective_vocal") for e in evals]),
        "visual": agg_visual([e.get("objective_visual") for e in evals]),
        "nonverbal": agg_nonverbal(windows),
        "analysis": collect_analysis(evals),
    }
    flags = flag_conflicts(evals)
    handoff["prompt_block"] = render_prompt_block(handoff, flags=flags)  # 줄 배열 — 소비자가 join
    handoff["meta"] = {
        "eval_windows": len(evals),
        "nv_windows": handoff["nonverbal"]["windows"],
        "stt_windows": sum(1 for w in windows if w.get("transcript", "").strip()),
        "transcript_chars": len(transcript),
        "prompt_block_chars": len("\n".join(handoff["prompt_block"])),
        "flags": flags,
    }
    return handoff


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("signals_json", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()
    payload = json.loads(args.signals_json.read_text())
    handoff = build_turn_handoff(payload)
    text = json.dumps(handoff, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
        print(f"wrote {args.out} ({handoff['meta']})")
    else:
        print(text)


if __name__ == "__main__":
    main()
