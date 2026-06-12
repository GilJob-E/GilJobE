"""turn_handoff v2 오프라인 회귀 — 집계 규칙·압축 6수치·prompt_block·signals_payload 배선.

소비자 LLM 위임 구조(2026-06-12)의 운반체 가드: eval 비평 없이도 문장 테이블+턴 집계+
판단 규칙이 소비자에게 도달해야 한다(GPU/vLLM 불요)."""
from __future__ import annotations

from giljobe.emit.handoff import build_turn_handoff
from giljobe.server.service import signals_payload


def _speech(dur: float, nuclei: int, artic: float, sd: float = 4.0, pauses: int = 0,
            energy_delta: float = -0.5) -> dict:
    return {
        "duration_s": dur,
        "pitch": {"voiced_frames": int(dur * 100), "voiced_ratio": 0.7, "mean_hz": 240.0,
                  "sd_semitone": sd, "pdq": 0.28, "range_st_10_90": 12.0,
                  "detrended_sd_semitone": sd, "octave_error_frac": 0.0},
        "rate": {"syllable_nuclei": nuclei, "speech_rate_syl_per_s": round(nuclei / dur, 2),
                 "articulation_rate_syl_per_s": artic},
        "pauses": {"pause_count_ge_0p25": pauses, "short_juncture_count_0p1_0p25": 3,
                   "juncture_median_s": 0.12, "longest_pauses_s": [0.3, 0.2]},
        "energy": {"rel_db_slope_per_s": -0.1, "half_to_half_delta_db": energy_delta,
                   "sounding_trough_db": -15.0},
    }


def _visual(frames: int, smile: float = 0.2, gaze: float = 0.3) -> dict:
    return {"face_frames": frames, "face_seen_ratio": 1.0, "smile_mean": smile, "smile_max": 0.7,
            "smile_ratio": 0.4, "brow_down_mean": 0.02, "mouth_press_mean": 0.02, "blink_count": 1,
            "gaze_off_mean": gaze, "yaw_std_deg": 0.8, "pitch_std_deg": 0.9, "lip_motion": 0.01,
            "pose_frames": frames, "pose_seen_ratio": 1.0, "posture_std": 0.01, "head_sway": 0.01,
            "wrist_visible": 0.0, "wrist_motion": 0.0, "gesture_events": 0}


def _sentence(start: float, end: float, text: str, *, speech: dict | None, visual: dict | None,
              nv: dict | None) -> dict:
    return {"type": "sentence", "session_id": "s", "t": end, "start_s": start, "end_s": end,
            "text": text, "source": "punct", "speech": speech, "visual": visual, "nonverbal": nv}


TURN_END = {"type": "turn_end", "session_id": "s", "t": 20.0, "transcript_full": "가나다. 라마바. 사아자.",
            "eval": None}

SENTS = [
    _sentence(0.5, 4.5, "가나다.", speech=_speech(4.0, 24, 6.0),
              visual=_visual(80), nv={"state": "neutral", "intensity": 0.3, "note": "미소"}),
    _sentence(4.5, 12.5, "라마바.", speech=_speech(8.0, 40, 5.0, pauses=2, energy_delta=-2.5),
              visual=_visual(160, smile=0.4), nv={"state": "engaged", "intensity": 0.6, "note": "시선 안정"}),
    _sentence(12.5, 13.3, "사아자.", speech=None, visual=None, nv=None),  # 초단문 — 레인 결손
]


def test_handoff_none_before_turn_end():
    assert build_turn_handoff("s", SENTS) is None


def test_handoff_v2_sentence_table_and_aggregates():
    h = build_turn_handoff("s", [*SENTS, TURN_END])
    assert h["schema_version"] == 2 and "analysis" not in h  # eval 비평 섹션은 v2에서 삭제
    assert h["transcript"] == "가나다. 라마바. 사아자."

    # 문장 테이블: 압축 6수치 + nv, 결손 레인은 None
    assert [r["text"] for r in h["sentences"]] == ["가나다.", "라마바.", "사아자."]
    r0, _, r2 = h["sentences"]
    assert r0["artic_syl_s"] == 6.0 and r0["f0_sd_st"] == 4.0 and r0["pause_count"] == 0
    assert r0["energy_delta_db"] == -0.5 and r0["smile"] == 0.2 and r0["gaze_off"] == 0.3
    assert r2["artic_syl_s"] is None and r2["smile"] is None and r2["nv"] is None

    # 집계: 카운트 합산·비율 재계산(24+40 음절 / 12s), 극값 병합
    assert h["speech"]["rate"]["syllable_nuclei"] == 64
    assert h["speech"]["rate"]["speech_rate_syl_per_s"] == round(64 / 12.0, 2)
    assert h["speech"]["pauses"]["pause_count_ge_0p25"] == 2
    assert h["visual"]["face_frames"] == 240
    assert h["nonverbal"]["windows"] == 2 and h["nonverbal"]["states"]["neutral"] == 0.5

    # meta 충족률 — 소비자의 신뢰도 신호
    assert h["meta"]["coverage"] == {"speech": 2, "visual": 2, "nv": 2}
    assert h["meta"]["sentences"] == 3


def test_handoff_prompt_block_sections_and_highlight():
    h = build_turn_handoff("s", [*SENTS, TURN_END])
    block = "\n".join(h["prompt_block"])
    assert "[음성 전달 실측치" in block and "[시각 실측치" in block
    assert "[판단 규칙]" in block and "네가 전사를 근거로 직접" in block
    # 2번 문장(에너지 -2.5dB·쉼 2회·nv engaged)이 하이라이트로 떠야 한다
    assert "[문장 하이라이트" in block and "라마바" in block and "에너지 -2.5dB" in block


def test_handoff_fallback_to_eval_objectives_without_sentences():
    """(구)wall 구성 폴백 — sentence 없이 eval objective_*만 있어도 집계는 나온다."""
    evals = [{"type": "eval", "session_id": "s", "t": 16.0,
              "eval": {"window_start_s": 0.0, "window_dur_s": 16.0,
                       "objective_vocal": _speech(16.0, 90, 5.8), "objective_visual": _visual(16)}}]
    h = build_turn_handoff("s", [*evals, TURN_END])
    assert h["sentences"] == []
    assert h["speech"]["rate"]["syllable_nuclei"] == 90 and h["visual"]["face_frames"] == 16


def test_signals_payload_carries_turn_handoff():
    before = signals_payload("s", SENTS)
    after = signals_payload("s", [*SENTS, TURN_END])
    assert before["turnHandoff"] is None
    assert after["turnHandoff"]["type"] == "turn_handoff"
    assert after["rawMediaExposed"] is False and after["rawSecretsExposed"] is False
