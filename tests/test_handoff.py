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


def test_render_prompt_fragment_single_line_budget_no_quotes():
    """Realtime fragment — 900자 예산 내 단일라인, 전사 인용 없음(시간 참조+수치만)."""
    from giljobe.emit.handoff import render_prompt_fragment

    h = build_turn_handoff("s", [*SENTS, TURN_END])
    frag = render_prompt_fragment(h)
    assert len(frag) <= 880 and "\n" not in frag
    assert "음성 실측" in frag and "조음" in frag
    assert "라마바" not in frag  # 전사 인용 금지 — 시간 참조만
    assert "(4~12초)" in frag and "에너지 -2.5dB" in frag
    assert "실측치만 정본" in frag


def test_agg_visual_merges_finger_segments_across_windows():
    """경계에서 같은 카운트로 이어지는 구간은 병합 — 손 레인 없는 윈도우 섞여도 무영향."""
    from giljobe.emit.handoff import agg_visual

    v1 = {**_visual(80), "hand_frames": 80, "hand_seen_ratio": 0.5,
          "finger_segments": [{"count": 3, "start_s": 1.0, "end_s": 2.0},
                              {"count": 2, "start_s": 2.5, "end_s": 3.5}]}
    v2 = {**_visual(80), "hand_frames": 80, "hand_seen_ratio": 0.5,
          "finger_segments": [{"count": 2, "start_s": 3.5, "end_s": 4.2},
                              {"count": 5, "start_s": 5.0, "end_s": 6.0}]}
    agg = agg_visual([v1, v2])
    assert [s["count"] for s in agg["finger_segments"]] == [3, 2, 5]
    assert agg["finger_sequence"] == [3, 2, 5]
    assert agg_visual([_visual(80)]) is not None  # 손 키 없는 구 shape — hand 키 미오염
    assert "hand_frames" not in agg_visual([_visual(80)])


def test_fragment_carries_finger_sequence_and_whisper_signals():
    """Realtime fragment에 손가락 시퀀스 + 무성(속삭임형) 신호가 실린다 — 기준 파일 2종 운반 가드."""
    from giljobe.emit.handoff import render_prompt_fragment

    sp = _speech(4.0, 0, 0.0)
    sp["pitch"] = {"voiced_frames": 2, "voiced_ratio": 0.03}
    sp["rate"] = {"syllable_nuclei": 0, "speech_rate_syl_per_s": 0.0,
                  "articulation_rate_syl_per_s": 0.0, "phonation_s": 3.0,
                  "intensity_peak_nuclei": 9, "intensity_peak_artic_per_s": 3.0}
    vis = {**_visual(80), "hand_frames": 80, "hand_seen_ratio": 0.6,
           "finger_segments": [{"count": 3, "start_s": 1.0, "end_s": 2.0},
                               {"count": 2, "start_s": 2.5, "end_s": 3.5},
                               {"count": 5, "start_s": 4.0, "end_s": 5.0}]}
    h = build_turn_handoff("s", [_sentence(0.5, 4.5, "가나다.", speech=sp, visual=vis, nv=None), TURN_END])
    frag = render_prompt_fragment(h)
    assert "손가락 펼침 3→2→5" in frag
    assert "무성 발성(유성핵 0)" in frag and "3.0/s" in frag
    assert "유성 비율 3%(무성 우세" in frag
    assert "\n" not in frag and len(frag) <= 880


def test_fragment_whisper_not_masked_by_sparse_f0():
    """속삭임(유성 비율<15%)인데 유성 프레임이 ≥5개라 sd_semitone이 계산된 케이스(.audio_test
    실측: voiced_ratio 0.026·sd 1.47·nuclei 3). 저신뢰 F0가 속삭임 신호를 가리지 않아야 한다."""
    from giljobe.emit.handoff import render_prompt_fragment

    sp = _speech(3.4, 3, 1.07)
    sp["pitch"] = {"voiced_frames": 50, "voiced_ratio": 0.026, "mean_hz": 472.7,
                   "sd_semitone": 1.47, "pdq": 0.09, "range_st_10_90": 4.2,
                   "detrended_sd_semitone": 1.41, "octave_error_frac": 0.0}
    h = build_turn_handoff("s", [_sentence(0.0, 5.37, "가나다.", speech=sp, visual=None, nv=None), TURN_END])
    frag = render_prompt_fragment(h)
    assert "속삭임형 발성" in frag and "유성 비율 2%" in frag   # 속삭임 신호가 표면화
    assert "F0 변동 1.47st" not in frag                        # 저신뢰 F0는 정상 억양처럼 안 나감
    assert "조음 1.07음절/s" in frag                           # 느린 발화는 그대로


def test_fragment_face_not_seen_says_undetected_not_none():
    """캠에 얼굴이 없으면(face_seen_ratio=0, face_frames>0) fragment가 '미소 평균 None'이 아니라
    '얼굴 미검출'을 실어 소비자 LLM이 '얼굴이 보인다'로 오독하지 않게 한다 — face_frames만 보던
    가드 버그 회귀 가드(라이브에서 빈 캠인데 면접관이 얼굴 보인다고 답한 사건)."""
    from giljobe.emit.handoff import render_prompt_fragment

    # grounder가 빈 캠(얼굴 미검출)에서 내는 윈도우 shape: _agg_lane det_n=0 → smile_mean 키 없음
    win = {"face_frames": 30, "face_seen_ratio": 0.0, "pose_frames": 30, "pose_seen_ratio": 0.0}
    h = build_turn_handoff("s", [_sentence(0.0, 5.0, "가나다.", speech=None, visual=win, nv=None), TURN_END])
    frag = render_prompt_fragment(h)
    assert "얼굴 미검출" in frag                  # 미검출을 명시
    assert "None" not in frag                     # '미소 평균 None'/'시선 이탈 None'이 새지 않음
    assert "미소 평균" not in frag                # 표정 수치는 검출 시에만
