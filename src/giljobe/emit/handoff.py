"""턴 핸드오프 v2 — 레코드를 턴 단위 `turnHandoff`로 집계(소비자 LLM 위임 구조의 운반체).

eval 제거 결정(2026-06-12) 이후의 분업: 발화 중은 측정(객관 레인)+nv 의미 읽기만 남고,
내용 판단은 다음 질문을 만드는 소비자 LLM이 이 핸드오프를 읽고 직접 한다. 그래서 v1의
analysis 섹션(eval 비평)이 빠지고 **sentences 테이블이 본문으로 승격**된다 — "어떤 내용을
말할 때 전달이 어떻게 변했나"의 시간 정렬 정보가 다음 질문의 재료다.

원칙(프로토타입 .dev/llm_handoff/make_turn_handoff.py에서 검증·포팅):
- 집계는 순수 파이썬, LLM 호출 0(★실시간 1순위에 비용 0). signals_payload에서 lazy 계산.
- 집계 shape = per-구간 shape → describe_vocal/describe_visual을 그대로 재사용(드리프트 0).
- 집계 규칙: 카운트=합산 / 비율=합산 후 재계산 / 분포 통계=가중평균 / 극값=병합.
- transcript 무절단 — 절단은 소비자 정책이지 스키마가 아니다.
- 문장당 수치는 압축 6개(조음속도·F0 sd·쉼·에너지 델타·smile·gaze — 2026-06-12 확정).
  풀 shape는 sentence 레코드에 이미 있으므로 핸드오프는 해석용 최소만 운반한다.
"""
from __future__ import annotations

from collections import Counter

from giljobe.analysis.grounding import describe_visual
from giljobe.analysis.prosody import describe_vocal

SCHEMA_VERSION = 2
MAX_HIGHLIGHTS = 3  # prompt_block 문장 하이라이트 상한(토큰 예산)


def _wmean(pairs: list[tuple[float | None, float]], nd: int) -> float | None:
    """가중평균 — 값 None/가중치 0은 건너뜀(관측 없던 구간이 평균을 끌어내리지 않게)."""
    valid = [(v, w) for v, w in pairs if v is not None and w]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    return round(sum(v * w for v, w in valid) / total_w, nd)


# ── speech 집계(구간 shape 동일 → describe_vocal 재사용 가능) ─────────────────

def _agg_pitch(ms: list[dict]) -> dict:
    """유성 프레임 가중평균. 풀링 SD가 아닌 '구간 내 변동의 평균' — 풀링하면 구간 간
    선언적 하강(declination)까지 섞여 억양 생동감이 인위 팽창한다."""
    valid = [(m["pitch"], m["duration_s"]) for m in ms if "sd_semitone" in m.get("pitch", {})]
    if not valid:
        # 피치는 못 내도 voiced_ratio는 운반한다 — 무성 우세 발성(속삭임형)의 턴 단위 신호
        out = {"note": "유성 구간 부족(피치 측정 불가)"}
        vr = _wmean([(m["pitch"].get("voiced_ratio"), m.get("duration_s", 0))
                     for m in ms if m.get("pitch")], 3)
        if vr is not None:
            out["voiced_ratio"] = vr
        return out
    out: dict = {"voiced_frames": sum(p["voiced_frames"] for p, _ in valid)}
    out["voiced_ratio"] = _wmean([(p.get("voiced_ratio"), d) for p, d in valid], 3)
    frames = [(p, p["voiced_frames"]) for p, _ in valid]
    for key, nd in [("mean_hz", 1), ("sd_semitone", 2), ("pdq", 4), ("range_st_10_90", 2),
                    ("detrended_sd_semitone", 2), ("octave_error_frac", 3)]:
        out[key] = _wmean([(p.get(key), w) for p, w in frames], nd)
    return out


def _agg_rate(ms: list[dict], dur: float) -> dict:
    """비율은 평균 내지 않고 합산 후 재계산 — 짧은 꼬리 구간이 같은 가중을 받는 함정 회피."""
    nuclei = sum(m.get("rate", {}).get("syllable_nuclei", 0) for m in ms)
    peaks = sum(m.get("rate", {}).get("intensity_peak_nuclei", 0) for m in ms)
    phonation = 0.0
    for m in ms:
        r = m.get("rate", {})
        if r.get("phonation_s"):  # 신 스키마 — 합산 가능 필드가 정본
            phonation += r["phonation_s"]
            continue
        artic = r.get("articulation_rate_syl_per_s") or 0
        if artic > 0:  # 구 레코드 폴백 — nuclei/artic로 역산(무성 구간은 복원 불가)
            phonation += r.get("syllable_nuclei", 0) / artic
    out = {
        "syllable_nuclei": nuclei,
        "speech_rate_syl_per_s": round(nuclei / dur, 2) if dur else None,
        "articulation_rate_syl_per_s": round(nuclei / phonation, 2) if phonation else None,
    }
    if peaks:
        out["intensity_peak_nuclei"] = peaks
        out["intensity_peak_artic_per_s"] = round(peaks / phonation, 2) if phonation else None
    return out


def _agg_pauses(ms: list[dict]) -> dict:
    """카운트 합산 + 최장 쉼은 전 구간 병합 top5. 중앙값은 정확 풀링 불가 → run 수 가중 근사."""
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
    """dB가 구간 상대값(피크=0)이라 절대 비교 불가 — 구간 내 추세만 길이 가중평균."""
    valid = [(m["energy"], m["duration_s"]) for m in ms if "half_to_half_delta_db" in m.get("energy", {})]
    if not valid:
        return {"note": "측정 불가"}
    troughs = [e["sounding_trough_db"] for e, _ in valid if e.get("sounding_trough_db") is not None]
    return {
        "rel_db_slope_per_s": _wmean([(e.get("rel_db_slope_per_s"), d) for e, d in valid], 3),
        "half_to_half_delta_db": _wmean([(e.get("half_to_half_delta_db"), d) for e, d in valid], 2),
        "sounding_trough_db": round(min(troughs), 2) if troughs else None,
    }


def agg_vocal(metrics: list[dict | None]) -> dict | None:
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


# ── visual 집계 ───────────────────────────────────────────────────────────────

FACE_KEYS = [("smile_mean", 3), ("smile_ratio", 2), ("brow_down_mean", 3), ("mouth_press_mean", 3),
             ("gaze_off_mean", 3), ("yaw_std_deg", 2), ("pitch_std_deg", 2), ("lip_motion", 5)]
POSE_KEYS = [("posture_std", 4), ("head_sway", 4), ("wrist_visible", 2), ("wrist_motion", 4)]


def _agg_lane(ms: list[dict], frames_key: str, ratio_key: str, keys: list[tuple[str, int]],
              sums: list[str], maxes: list[str]) -> dict:
    """face/pose 공통 집계 — 검출 프레임 수를 가중치로, 미검출 구간은 coverage에만 반영."""
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


def agg_visual(metrics: list[dict | None]) -> dict | None:
    ms = [m for m in metrics if m]
    if not ms:
        return None
    out: dict = {"windows": len(ms)}
    out.update(_agg_lane(ms, "face_frames", "face_seen_ratio", FACE_KEYS,
                         sums=["blink_count"], maxes=["smile_max"]))
    out.update(_agg_lane(ms, "pose_frames", "pose_seen_ratio", POSE_KEYS,
                         sums=["gesture_events"], maxes=[]))
    if any("hand_frames" in m for m in ms):  # 손 레인 켜진 배포에서만 키 추가(구 shape 보존)
        out.update(_agg_lane(ms, "hand_frames", "hand_seen_ratio", [], sums=[], maxes=[]))
        segs = _merge_finger_segments(ms)
        if segs:
            out["finger_segments"] = segs
            out["finger_sequence"] = [s["count"] for s in segs if s["count"]]
    return out


def _merge_finger_segments(ms: list[dict]) -> list[dict]:
    """윈도우별 finger_segments를 시간순 연결 — 경계에서 같은 카운트로 이어지면 한 구간으로
    (집계 shape = per-구간 shape 원칙: describe_visual을 그대로 재사용)."""
    segs = sorted((s for m in ms for s in m.get("finger_segments", [])),
                  key=lambda s: s["start_s"])
    merged: list[dict] = []
    for s in segs:
        if merged and merged[-1]["count"] == s["count"]:
            merged[-1] = {**merged[-1], "end_s": s["end_s"]}
        else:
            merged.append(dict(s))
    return merged


def agg_nonverbal(nvs: list[dict]) -> dict:
    """nv read의 상태 분포 — 객관 수치는 visual 집계가 정본이므로 여기선 모델 read만 요약."""
    if not nvs:
        return {"windows": 0}
    states = Counter(nv["state"] for nv in nvs)
    return {
        "windows": len(nvs),
        "states": {s: round(c / len(nvs), 2) for s, c in states.most_common()},
        "intensity_mean": round(sum(nv.get("intensity", 0) for nv in nvs) / len(nvs), 2),
        "notes": [nv["note"] for nv in nvs if nv.get("note")][-3:],  # 최근 3개(턴 후반 상태)
    }


# ── sentences 테이블(v2 본문) + 하이라이트 ───────────────────────────────────

def _sentence_row(r: dict) -> dict:
    """문장 1행 — 압축 수치 6개(없는 레인은 None). 풀 shape는 sentence 레코드가 정본."""
    sp, vis = r.get("speech") or {}, r.get("visual") or {}
    nv = r.get("nonverbal") or None
    return {
        "start_s": r.get("start_s"),
        "end_s": r.get("end_s"),
        "text": r.get("text") or "",
        "artic_syl_s": (sp.get("rate") or {}).get("articulation_rate_syl_per_s"),
        "f0_sd_st": (sp.get("pitch") or {}).get("sd_semitone"),
        "pause_count": (sp.get("pauses") or {}).get("pause_count_ge_0p25"),
        "energy_delta_db": (sp.get("energy") or {}).get("half_to_half_delta_db"),
        "smile": vis.get("smile_mean"),
        "gaze_off": vis.get("gaze_off_mean"),
        "nv": nv,
    }


def _highlight_score(row: dict, artic_mean: float | None) -> int:
    """주목 문장 점수 — 결정적 휴리스틱(LLM 0): 에너지 급변·진짜 쉼·비중립 nv·속도 이탈."""
    score = 0
    e = row.get("energy_delta_db")
    if e is not None and abs(e) >= 2.0:
        score += 1
    if (row.get("pause_count") or 0) >= 2:
        score += 1
    nv = row.get("nv") or {}
    if nv and (nv.get("state") not in (None, "neutral") or (nv.get("intensity") or 0) >= 0.5):
        score += 1
    a = row.get("artic_syl_s")
    if a is not None and artic_mean is not None and abs(a - artic_mean) > 1.0:
        score += 1
    return score


def _render_highlights(rows: list[dict]) -> list[str]:
    artics = [r["artic_syl_s"] for r in rows if r.get("artic_syl_s") is not None]
    artic_mean = sum(artics) / len(artics) if artics else None
    scored = sorted(((_highlight_score(r, artic_mean), r) for r in rows),
                    key=lambda x: x[0], reverse=True)
    lines = []
    for score, r in scored[:MAX_HIGHLIGHTS]:
        if score <= 0:
            break
        bits = []
        if r.get("energy_delta_db") is not None and abs(r["energy_delta_db"]) >= 2.0:
            bits.append(f"에너지 {r['energy_delta_db']:+.1f}dB")
        if (r.get("pause_count") or 0) >= 2:
            bits.append(f"쉼 {r['pause_count']}회")
        nv = r.get("nv") or {}
        if nv and nv.get("state") not in (None, "neutral"):
            bits.append(f"nv {nv['state']}")
        if r.get("artic_syl_s") is not None and artic_mean is not None \
                and abs(r["artic_syl_s"] - artic_mean) > 1.0:
            bits.append(f"조음 {r['artic_syl_s']}음절/s(턴 평균 {artic_mean:.1f})")
        text = (r.get("text") or "")[:40]
        lines.append(f"- ({int(r['start_s'] or 0)}~{int(r['end_s'] or 0)}초) \"{text}\" — {', '.join(bits)}")
    return lines


# ── prompt_block v2 + 조립 ────────────────────────────────────────────────────

# 비평 섹션이 사라진 자리의 소비자 지침 — 판단 주체 이전 + 실측 우선 + 관측불가 차단.
CONSUMER_RULES = [
    "내용(논리·구조·구체성)의 판단은 이 블록을 읽는 네가 전사를 근거로 직접 하라.",
    "전달력(억양·속도·쉼·음량·시선·자세)은 위 실측치가 정본 — 실측과 다른 인상 묘사 금지"
    "(예: F0 표준편차 2 semitone 미만일 때만 '단조').",
    "비언어 노트는 모델의 보조 관찰 — 실측치와 충돌하면 실측치를 따를 것.",
    "'관측 불가' 항목은 평가하지 말 것(없다고 단정하는 것도 금지).",
]


def render_prompt_block(handoff: dict) -> list[str]:
    """ai-engine이 '\\n'.join으로 끼울 한국어 블록(전사 제외 — lastAnswer는 기존 경로 유지).
    JSON에는 줄 배열로 싣는다(QA에서 사람이 바로 읽게). describe_* 재사용(차단 문구 포함)."""
    lines: list[str] = []

    def section(header: str, body: list[str]) -> None:
        if not body:
            return
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
        section(f"[비언어 상태 분포 — read {nv['windows']}개]",
                [f"- 상태: {states} · 평균 강도 {nv['intensity_mean']}"])
    section("[문장 하이라이트 — 전달이 변한 구간]", _render_highlights(handoff.get("sentences", [])))
    section("[판단 규칙]", [f"- {r}" for r in CONSUMER_RULES])
    return lines


def render_prompt_fragment(handoff: dict, max_chars: int = 880) -> str:
    """Realtime 경로용 단일라인 압축 fragment — giljob-docker API의 candidatePromptFragment
    예산(900자, 공백 정규화로 개행 불가) 안에서 핵심만 운반한다. 전사 인용은 싣지 않는다
    (candidate-safe 보수 설계 — 시간 참조+수치+nv 상태만). prompt_block의 축약본."""
    parts: list[str] = []
    sp = handoff.get("speech") or {}
    if sp:
        pitch, rate, pauses, energy = (sp.get(k, {}) for k in ("pitch", "rate", "pauses", "energy"))
        bits = []
        if rate.get("syllable_nuclei") == 0 and rate.get("intensity_peak_artic_per_s"):
            bits.append(f"무성 발성(유성핵 0) — 강도 피크 {rate['intensity_peak_artic_per_s']}/s 참고")
        elif rate.get("articulation_rate_syl_per_s") is not None:
            bits.append(f"조음 {rate['articulation_rate_syl_per_s']}음절/s(보통 5.8~6.9)")
        if pitch.get("sd_semitone") is not None:
            bits.append(f"F0 변동 {pitch['sd_semitone']}st(2 미만이면 단조)")
        elif (vr := pitch.get("voiced_ratio")) is not None and vr < 0.15:
            bits.append(f"유성 비율 {int(vr * 100)}%(무성 우세 — 속삭임형 발성)")
        if pauses.get("pause_count_ge_0p25") is not None:
            bits.append(f"쉼(0.25s 이상) {pauses['pause_count_ge_0p25']}회")
        if energy.get("half_to_half_delta_db") is not None:
            bits.append(f"음량 전반 대비 후반 {energy['half_to_half_delta_db']:+.1f}dB")
        if bits:
            parts.append("음성 실측: " + " · ".join(bits) + ".")
    vis = handoff.get("visual") or {}
    if vis.get("face_frames"):
        bits = [f"미소 평균 {vis.get('smile_mean')}", f"시선 이탈 {vis.get('gaze_off_mean')}"]
        if vis.get("finger_sequence"):
            bits.append("손가락 펼침 " + "→".join(str(c) for c in vis["finger_sequence"]))
        if (vis.get("wrist_visible") or 0) < 0.2:
            bits.append("손동작 관측 불가(평가 금지)")
        parts.append("시각 실측: " + " · ".join(str(b) for b in bits) + ".")
    nv = handoff.get("nonverbal") or {}
    if nv.get("windows"):
        states = ", ".join(f"{s} {int(r * 100)}%" for s, r in nv["states"].items())
        parts.append(f"비언어 상태: {states}.")
    hi_bits = []
    for line in _render_highlights(handoff.get("sentences", [])):
        # "- (30~37초) \"...\" — 에너지 -2.0dB" → 인용 제거, 시간+수치만
        time_ref, _, metrics = line.partition(" — ")
        time_ref = time_ref.split('"')[0].lstrip("- ").strip()
        if metrics:
            hi_bits.append(f"{time_ref} {metrics}")
    if hi_bits:
        parts.append("주목 구간: " + " / ".join(hi_bits) + ".")
    parts.append("내용 판단은 들은 답변 기준으로 직접 하고, 전달력은 위 실측치만 정본으로 삼아라.")
    text = " ".join(parts)
    return text[:max_chars]


def build_turn_handoff(session_id: str, records: list[dict]) -> dict | None:
    """turn_end가 있어야 빌드(턴 완결 전 None). external 모드는 sentence 레코드가 정본 입력,
    (구)internal·wall 구성에서는 eval objective_*/window nv로 폴백 — additive 호환."""
    turn_ends = [r for r in records if r.get("type") == "turn_end"]
    if not turn_ends:
        return None
    latest_te = turn_ends[-1]
    sents = [r for r in records if r.get("type") == "sentence"]
    evals = [r["eval"] for r in records if r.get("type") == "eval" and r.get("eval")]
    windows = [r for r in records if r.get("type") == "window"]

    rows = [_sentence_row(r) for r in sorted(sents, key=lambda r: r.get("start_s") or 0)]
    vocal_in = [r.get("speech") for r in sents] or [e.get("objective_vocal") for e in evals]
    visual_in = [r.get("visual") for r in sents] or [e.get("objective_visual") for e in evals]
    nv_in = ([r["nonverbal"] for r in sents if r.get("nonverbal")]
             or [w["nonverbal"] for w in windows if w.get("nonverbal")])
    transcript = latest_te.get("transcript_full") or " ".join(
        w["transcript"] for w in windows if w.get("transcript", "").strip())

    handoff = {
        "type": "turn_handoff",
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "t": latest_te.get("t"),
        "transcript": transcript,  # 무절단 — 절단은 소비자 정책
        "sentences": rows,
        "speech": agg_vocal(vocal_in),
        "visual": agg_visual(visual_in),
        "nonverbal": agg_nonverbal(nv_in),
    }
    handoff["prompt_block"] = render_prompt_block(handoff)  # 줄 배열 — 소비자가 join
    handoff["meta"] = {
        "sentences": len(rows),
        "coverage": {
            "speech": sum(1 for r in rows if r["artic_syl_s"] is not None),
            "visual": sum(1 for r in rows if r["smile"] is not None),
            "nv": sum(1 for r in rows if r["nv"]),
        },
        "transcript_chars": len(transcript),
        "prompt_block_chars": len("\n".join(handoff["prompt_block"])),
    }
    return handoff
