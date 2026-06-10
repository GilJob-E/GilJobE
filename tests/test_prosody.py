"""test_prosody — 프로소디 그라운딩 레인 오프라인 검증 (GPU/vLLM 불요, CPU only).

합성 신호로 4신호(단조로움/속도/쉼/볼륨)의 *방향*을 검증한다 — 절대값은 녹음/코덱에 민감하므로
순서 관계와 검출 여부만 assert(실측 절대값 검증은 .dev/audio PoC evidence가 정본). 실패/엣지
(짧은 입력·무음)를 먼저 본다(rules/testing.md). 실 클립 검증은 kor 클립 게이트로 분리.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("parselmouth", reason="prosody extra 미설치([prosody])")

from giljobe.analysis.prosody import ProsodyExtractor, describe_vocal, maybe_prosody_extractor

SR = 16000


def _pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _tone(dur: float, *, f0: float = 200.0, sweep_st: float = 0.0, gain=None) -> np.ndarray:
    """음절 모사 진폭변조(4Hz, 바닥 -14dB=묵음 임계 위) 톤. sweep_st: ±세미톤 피치 스윕(0.5Hz)."""
    t = np.arange(int(dur * SR)) / SR
    if sweep_st:
        f = f0 * 2.0 ** (sweep_st / 12.0 * np.sin(2 * np.pi * 0.5 * t))
        phase = 2 * np.pi * np.cumsum(f) / SR
    else:
        phase = 2 * np.pi * f0 * t
    am = 0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t)
    y = 0.5 * am * np.sin(phase)
    if gain is not None:
        y = y * gain(t)
    return y


# ─────────────────────────── 실패/엣지 먼저 ───────────────────────────

def test_too_short_returns_none():
    ex = ProsodyExtractor()
    assert ex.extract(b"") is None
    assert ex.extract(b"\x00\x00" * 100) is None  # 1s 미만


def test_silence_degrades_gracefully():
    """무음 4s — 죽지 않고 '측정 불가'가 드러나는 dict를 낸다(레인 실패≠파이프라인 실패)."""
    m = ProsodyExtractor().extract(b"\x00\x00" * (SR * 4))
    assert m is not None
    assert m["pitch"]["voiced_frames"] == 0
    assert m["rate"]["syllable_nuclei"] == 0
    assert "note" in m["energy"]
    # 무음 수치의 describe는 단정 라벨을 만들지 않는다
    assert "단조" not in describe_vocal(m).replace("2 미만이면 단조", "")


# ─────────────────────────── 4신호 방향성 ───────────────────────────

def test_pitch_variation_orders_monotone_vs_varied():
    """피치 스윕(±4st)이 고정 톤보다 sd_semitone이 크게 나와야 '단조로움' 그라운딩이 성립."""
    ex = ProsodyExtractor()
    mono = ex.extract(_pcm(_tone(4.0)))
    varied = ex.extract(_pcm(_tone(4.0, sweep_st=4.0)))
    assert mono["pitch"]["sd_semitone"] < 1.0
    assert varied["pitch"]["sd_semitone"] > 1.5
    assert varied["pitch"]["pdq"] > mono["pitch"]["pdq"]


def test_pause_detection_counts_inserted_silences():
    """0.5s 묵음 2개 삽입 → 진짜 쉼(≥0.25s) 2개 이상 검출(선행/후행 묵음은 제외)."""
    seg = _tone(0.8)
    gap = np.zeros(int(0.5 * SR))
    y = np.concatenate([seg, gap, seg, gap, seg])
    m = ProsodyExtractor().extract(_pcm(y))
    assert m["pauses"]["pause_count_ge_0p25"] >= 2
    # 연속 발화(쉼 0)는 진짜 쉼이 없어야 함
    m2 = ProsodyExtractor().extract(_pcm(_tone(3.0)))
    assert m2["pauses"]["pause_count_ge_0p25"] == 0


def test_energy_fade_yields_negative_delta():
    """후반으로 갈수록 작아지는 신호 → half_to_half_delta_db 음수('볼륨 하락' 그라운딩)."""
    fade = ProsodyExtractor().extract(_pcm(_tone(4.0, gain=lambda t: 1.0 - 0.7 * t / t[-1])))
    flat = ProsodyExtractor().extract(_pcm(_tone(4.0)))
    assert fade["energy"]["half_to_half_delta_db"] < -3.0
    assert abs(flat["energy"]["half_to_half_delta_db"]) < 1.5


def test_syllable_rate_tracks_am_modulation():
    """4Hz 진폭변조 ≈ 음절핵 ~4/s — 검출기가 자릿수 수준에서 추적해야 발화속도 그라운딩 성립."""
    m = ProsodyExtractor().extract(_pcm(_tone(4.0)))
    assert 2.0 <= m["rate"]["speech_rate_syl_per_s"] <= 6.0


# ─────────────────────────── describe(주입 라벨) ───────────────────────────

def test_describe_vocal_labels_and_agc_caveat():
    m = ProsodyExtractor().extract(_pcm(_tone(4.0, sweep_st=3.0)))
    text = describe_vocal(m)
    assert "억양 변화" in text and "음절/초" in text and "쉼" in text
    assert "AGC" in text  # 절대 음량 주장 차단 단서
    assert "긴장" not in text  # 해석 라벨 금지(필수 조건 2)


def test_describe_vocal_empty_metrics():
    assert describe_vocal({}) == ""


# ─────────────────────────── 팩토리/성능 ───────────────────────────

def test_maybe_factory_env_off(monkeypatch):
    monkeypatch.setenv("GILJOBE_PROSODY", "off")
    assert maybe_prosody_extractor() is None
    monkeypatch.setenv("GILJOBE_PROSODY", "auto")
    assert maybe_prosody_extractor() is not None


def test_latency_16s_window_under_budget():
    """16s 윈도우 추출이 0.5s 미만(실측 ~27ms, eval 추론 3–7s 대비 무시 가능해야 하는 설계 가정)."""
    ex = ProsodyExtractor()
    pcm = _pcm(_tone(16.0, sweep_st=2.0))
    ex.extract(pcm)  # 워밍업(라이브러리 1회 초기화 비용 제외)
    t0 = time.perf_counter()
    ex.extract(pcm)
    assert time.perf_counter() - t0 < 0.5


# ─────────────────────────── 실 클립(있으면) ───────────────────────────

def test_real_clip_matches_poc_direction():
    """kor 클립 0-16s — PoC evidence와 같은 방향(단조 아님·정상 속도·쉼 없음)이어야 한다."""
    pcm_path = Path(__file__).parent.parent / ".dev" / "audio" / "kor_16k_mono.pcm"
    if not pcm_path.is_file():
        pytest.skip("kor_16k_mono.pcm 없음(gitignore 산출물)")
    raw = pcm_path.read_bytes()[: 16 * SR * 2]
    m = ProsodyExtractor().extract(raw)
    assert m["pitch"]["sd_semitone"] > 2.0          # 젬마 "단조로운 톤" 반증 방향
    assert 5.0 <= m["rate"]["articulation_rate_syl_per_s"] <= 7.5  # 한국어 정상 범위
    assert m["pauses"]["pause_count_ge_0p25"] == 0  # "호흡 짧음"(유일 그라운딩) 방향
