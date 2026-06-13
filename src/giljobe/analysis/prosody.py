"""프로소디 그라운딩 레인 — 16k mono Int16 PCM 윈도우에서 객관 음향 수치 4종을 집계한다.

`.dev/audio/` 탐색 세션의 확정 결정 포팅(`prosody_grounding_poc.py` → 프로덕션):
  - 피치/단조로움: parselmouth(Praat AC, Hirst 2-pass floor/ceiling) → PDQ·F0 std(semitone)·detrend
  - 발화속도: 강도 피크(dip 기준)+유성성 게이트 음절핵(De Jong-Wempe 2009 본질) → speech/articulation rate
  - 쉼/호흡: Praat 강도 기반 silence run → 진짜 쉼(≥0.25s)·짧은 휴지(0.1~0.25s) 분포
  - 에너지/볼륨: RMS 컨투어 상대 dB 추세 — PoC의 librosa와 동일 정의(25ms 프레임/10ms hop,
    윈도우 피크=0dB)를 numpy로 직접 계산해 의존성을 줄인다. ★상대값만: WebRTC AGC가 절대
    음량을 교란하므로 절대 dB 주장은 만들지 않는다.

레인의 역할은 *판정*이 아니라 *근거 없는 단정 차단*이다: "단조로움·속도·볼륨·쉼"은 그라운딩
가능하지만 "긴장/자신감/열정"은 어떤 음향 수치로도 불가(.dev/audio/README.md 결정 표) —
해석 라벨을 붙이지 않고 기술적 사실만 집계한다. 전부 CPU·전사 비의존(실측 16s 윈도우 ~27ms)
→ eval 워커 스레드 안에서 동기 계산해도 이벤트루프·레인 cadence에 영향이 없다.

의존성: praat-parselmouth·scipy는 optional extra(`[prosody]`) — 모듈 import는 가볍게 유지하고
무거운 import는 extractor 내부(lazy)에서만 한다. ★parselmouth는 GPL-3(.dev/audio/README.md
통합 주의) — permissive 배포가 요구되면 pyworld 폴백을 그때 검토한다.
"""
from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MIN_DURATION_S = 1.0   # 이보다 짧으면 Praat 피치/강도 분석이 무의미 → extract는 None
RMS_FRAME = 400        # 25ms @16k — PoC(librosa frame_length=400)와 동일 정의
RMS_HOP = 160          # 10ms @16k
PAUSE_S = 0.25         # 진짜 쉼(운율 경계) 기준 — ~250ms norm
JUNCTURE_S = 0.1       # 짧은 휴지/호흡 하한


def maybe_prosody_extractor() -> "ProsodyExtractor | None":
    """프로소디 레인 팩토리 — 의존성이 없거나 env로 껐으면 None(레인 비활성, 파이프라인 무영향).
    env: GILJOBE_PROSODY=off 로 명시 비활성(기본 auto=의존성 있으면 켬)."""
    if os.environ.get("GILJOBE_PROSODY", "auto").lower() == "off":
        logger.info("프로소디 레인 비활성(GILJOBE_PROSODY=off)")
        return None
    try:
        import parselmouth  # noqa: F401
        import scipy  # noqa: F401
    except ImportError as exc:
        logger.info("프로소디 레인 비활성(의존성 없음: %s) — pip install 'giljobe[prosody]'", exc)
        return None
    return ProsodyExtractor()


class ProsodyExtractor:
    """한 윈도우 PCM → 객관 프로소디 수치 dict. 상태 없음(스레드 안전) — eval/tail 레인이 공유한다.

    silence_offset_db: 강도 99분위 대비 묵음 임계 오프셋. Praat 표준 25dB는 SNR 낮은 녹음에서
    어절 휴지를 못 잡아(PoC 프로브 검증) 18dB를 기본으로 한다. min_dip_db: 음절핵 prominence."""

    def __init__(self, *, silence_offset_db: float = 18.0, min_dip_db: float = 2.0) -> None:
        self.silence_offset_db = silence_offset_db
        self.min_dip_db = min_dip_db

    def extract(self, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> dict | None:
        """Int16 PCM → {"duration_s", "pitch", "rate", "pauses", "energy"}. 너무 짧거나(<1s)
        분석 실패면 None — 호출자(윈도워)는 None이면 주입/emit을 생략한다(레인 무영향)."""
        if len(pcm) < int(MIN_DURATION_S * sample_rate) * 2:
            return None
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
        try:
            import parselmouth

            snd = parselmouth.Sound(samples, sampling_frequency=sample_rate)
            rate, pauses = self._rate_and_pauses(snd)
            return {
                "duration_s": round(len(samples) / sample_rate, 2),
                "pitch": _pitch_metrics(snd),
                "rate": rate,
                "pauses": pauses,
                "energy": _energy_metrics(samples, sample_rate),
            }
        except Exception:  # noqa: BLE001 - 그라운딩 실패가 eval 레인을 죽이면 안 됨(수치만 누락)
            logger.exception("prosody extract 실패(레인 무영향, 이 윈도우 수치만 누락)")
            return None

    def _rate_and_pauses(self, snd) -> tuple[dict, dict]:
        """음절핵(강도 국소피크 ∧ sounding ∧ voiced) → 발화/조음속도, silence run → 쉼 분포."""
        from parselmouth.praat import call
        from scipy.signal import find_peaks

        intensity = snd.to_intensity(minimum_pitch=100.0)
        it = intensity.xs()
        idb = intensity.values[0]
        dur = snd.get_total_duration()
        threshold = call(intensity, "Get quantile", 0, 0, 0.99) - self.silence_offset_db

        peaks, _ = find_peaks(idb, prominence=self.min_dip_db)
        pitch = snd.to_pitch_ac()
        nuclei = 0
        peaks_above = 0  # 유성성 게이트 전 피크 — 무성 발성(속삭임)의 속도 참고치
        for pk in peaks:
            if idb[pk] < threshold:
                continue  # 묵음 구간 피크 제외
            peaks_above += 1
            f0 = pitch.get_value_at_time(it[pk])
            if f0 is None or np.isnan(f0) or f0 <= 0:
                continue  # 유성성 게이트(치찰음·잡음 피크 배제)
            nuclei += 1

        runs = _silence_runs(idb, it, threshold)
        pauses = [r for r in runs if r >= PAUSE_S]
        junctures = [r for r in runs if JUNCTURE_S <= r < PAUSE_S]
        phonation = max(dur - float(sum(runs)), 1e-6)
        rate = {
            "syllable_nuclei": nuclei,
            "speech_rate_syl_per_s": round(nuclei / dur, 2),              # 쉼 포함
            "articulation_rate_syl_per_s": round(nuclei / phonation, 2),  # 쉼 제외
            "phonation_s": round(phonation, 2),                           # 집계 재계산용(합산 가능)
            # 속삭임은 유성핵이 0으로 무너진다 — 무성 포함 피크 기준 속도를 참고치로 함께 싣는다
            "intensity_peak_nuclei": peaks_above,
            "intensity_peak_artic_per_s": round(peaks_above / phonation, 2),
        }
        pause_stats = {
            "pause_count_ge_0p25": len(pauses),
            "short_juncture_count_0p1_0p25": len(junctures),
            "juncture_median_s": round(float(np.median(runs)), 3) if runs else None,
            "longest_pauses_s": [round(r, 2) for r in sorted(runs, reverse=True)[:5]],
        }
        return rate, pause_stats


def _silence_runs(idb: np.ndarray, it: np.ndarray, threshold: float) -> list[float]:
    """강도<threshold 연속 구간 길이(초) — 선행/후행 묵음은 발화 외라 제외(내부 쉼만)."""
    dt = it[1] - it[0] if len(it) > 1 else 0.01
    sounding_idx = np.where(idb >= threshold)[0]
    if not sounding_idx.size:
        return []
    lo, hi = sounding_idx[0], sounding_idx[-1]
    silent = idb < threshold
    runs: list[float] = []
    run = 0
    for i in range(lo, hi + 1):
        if silent[i]:
            run += 1
        else:
            if run * dt >= JUNCTURE_S:
                runs.append(run * dt)
            run = 0
    return runs


def _pitch_metrics(snd) -> dict:
    """Hirst 2011 2-pass(광역 추정→25/75 분위 floor/ceiling 재설정)로 F0 → 단조로움 수치.
    세미톤 std는 ref 무관, declination detrend는 하강 추세를 제거한 억양 생동감."""
    floor, ceil = _pitch_floor_ceiling(snd)
    p = snd.to_pitch_ac(pitch_floor=floor, pitch_ceiling=ceil)
    t = p.xs()
    f = p.selected_array["frequency"]
    voiced = f > 0
    fv = f[voiced]
    tv = t[voiced]
    if fv.size < 5:
        # voiced_ratio는 여기서도 싣는다 — 발화 에너지가 있는데 유성이 희박하면 그 자체가
        # 무성 우세 발성(속삭임형)의 객관 신호다(피치 실패 ≠ 측정 실패).
        return {"voiced_frames": int(fv.size), "voiced_ratio": round(float(voiced.mean()), 3),
                "note": "유성 프레임 부족(피치 측정 불가)"}
    st = 12.0 * np.log2(fv)
    slope, intercept = np.polyfit(tv, st, 1)
    resid = st - (slope * tv + intercept)
    median = float(np.median(fv))
    return {
        "voiced_frames": int(fv.size),
        "voiced_ratio": round(float(voiced.mean()), 3),
        "mean_hz": round(float(fv.mean()), 1),
        "sd_semitone": round(float(st.std()), 2),
        "pdq": round(float(fv.std() / fv.mean()), 4),  # Pitch Dynamism Quotient (Hincks)
        "range_st_10_90": round(float(np.percentile(st, 90) - np.percentile(st, 10)), 2),
        "detrended_sd_semitone": round(float(resid.std()), 2),
        # 중앙값 1.9배(옥타브 근처) 초과 비율 — 높으면 옥타브 오류로 SD가 인위 팽창(신뢰 게이트)
        "octave_error_frac": round(float((fv > median * 1.9).mean()), 3),
    }


def _pitch_floor_ceiling(snd) -> tuple[float, float]:
    p1 = snd.to_pitch_ac(pitch_floor=60, pitch_ceiling=600)
    f = p1.selected_array["frequency"]
    f = f[f > 0]
    if f.size < 5:
        return 75.0, 500.0
    q25, q75 = np.percentile(f, [25, 75])
    return max(50.0, float(q25) * 0.75), min(600.0, float(q75) * 1.5)


def _energy_metrics(samples: np.ndarray, sample_rate: int) -> dict:
    """RMS 컨투어(25ms/10ms) → 상대 dB(윈도우 피크=0) 추세·전후반 차. sounding 프레임(상위 60%)만
    써서 쉼이 통계를 지배하지 않게 한다. PoC energy_metrics와 동일 정의(librosa→numpy)."""
    y = samples.astype(np.float64)
    n = 1 + max(0, (len(y) - RMS_FRAME) // RMS_HOP)
    if n < 8:
        return {"note": "프레임 부족"}
    idx = np.arange(RMS_FRAME)[None, :] + RMS_HOP * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(y[idx] ** 2, axis=1))
    peak = float(rms.max())
    if peak <= 1e-8:
        return {"note": "무음"}
    db = 20.0 * np.log10(np.maximum(rms, 1e-10) / peak)
    times = (RMS_HOP * np.arange(n)) / sample_rate
    sounding = db > np.percentile(db, 40)
    db_s, t_s = db[sounding], times[sounding]
    if db_s.size < 4:
        return {"note": "sounding 프레임 부족"}
    half = len(db_s) // 2
    first, second = float(np.median(db_s[:half])), float(np.median(db_s[half:]))
    return {
        "rel_db_slope_per_s": round(float(np.polyfit(t_s, db_s, 1)[0]), 3),
        "half_to_half_delta_db": round(second - first, 2),  # 음수=후반 작아짐(상대값)
        "sounding_trough_db": round(float(db_s.min()), 2),
    }


def silence_run_positions(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> list[tuple[float, float]]:
    """Int16 PCM → (시작초, 길이초) 휴지 목록 — 레인과 같은 정의(99분위-18dB, 25ms/10ms RMS).

    `_silence_runs`는 길이만 반환(쉼 *분포* 집계용)하지만, 문장 경계 스냅(pipeline/sentences.py)은
    *위치*가 필요해 모듈 함수로 노출한다. numpy만 사용(파셀마우스 불요) — 어디서든 가볍게 호출."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
    n = 1 + max(0, (len(samples) - RMS_FRAME) // RMS_HOP)
    if n < 8:
        return []
    idx = np.arange(RMS_FRAME)[None, :] + RMS_HOP * np.arange(n)[:, None]
    rms = np.sqrt(np.mean(samples[idx] ** 2, axis=1))
    peak = float(rms.max())
    if peak <= 1e-8:
        return []
    db = 20.0 * np.log10(np.maximum(rms, 1e-10) / peak)
    threshold = float(np.percentile(db, 99)) - 18.0
    silent = db < threshold
    runs: list[tuple[float, float]] = []
    start: int | None = None
    for i, s in enumerate(silent):
        if s and start is None:
            start = i
        elif not s and start is not None:
            dur = (i - start) * RMS_HOP / sample_rate
            if dur >= JUNCTURE_S:
                runs.append((start * RMS_HOP / sample_rate, dur))
            start = None
    return runs


def describe_vocal(m: dict) -> str:
    """프로소디 수치 → 프롬프트 주입용 한국어 라벨 라인(집계+참조범위, 해석 라벨 없음 —
    .dev/vision/README.md 필수 조건 2: "'긴장' 같은 해석 라벨은 미리 붙이지 않는다")."""
    lines: list[str] = []
    pitch, rate = m.get("pitch", {}), m.get("rate", {})
    pauses, energy = m.get("pauses", {}), m.get("energy", {})
    vr = pitch.get("voiced_ratio")
    whisperish = vr is not None and vr < 0.15
    # 속삭임 우선: 유성 비율<15%면 sd_semitone이 있어도 그 F0는 희박한 유성 프레임의 저신뢰값
    # (속삭임은 사실상 F0 부재)이라 속삭임 신호를 가린다 — 속삭임형을 먼저 싣고 F0는 강등한다.
    if whisperish:
        lines.append(
            f"- 억양: 무성 우세 발성(유성 비율 {int((vr or 0) * 100)}%) — 발화 에너지 대비 성대 진동 "
            "희박 = 속삭임형 신호. 피치 기반 평가 근거 약함"
            + (f"(F0 표준편차 {pitch['sd_semitone']} semitone은 신뢰 낮음)" if "sd_semitone" in pitch else "")
        )
    elif "sd_semitone" in pitch:
        lines.append(
            f"- 억양 변화: F0 표준편차 {pitch['sd_semitone']} semitone"
            f"(2 미만이면 단조), PDQ {pitch['pdq']}"
        )
    elif pitch:
        lines.append("- 억양: 유성 구간 부족 — 피치 기반 평가 근거 없음")
    if "articulation_rate_syl_per_s" in rate:
        if rate.get("syllable_nuclei") == 0 and rate.get("intensity_peak_nuclei"):
            lines.append(
                f"- 발화 속도: 유성 음절핵 0(무성 발성) — 강도 피크 기준 "
                f"{rate['intensity_peak_artic_per_s']} 피크/초(참고치, "
                "표준 음절 척도 5.8~6.9와 직접 비교 불가)"
            )
        else:
            lines.append(
                f"- 발화 속도: 조음 {rate['articulation_rate_syl_per_s']} 음절/초"
                f"(한국어 보통 5.8~6.9), 쉼 포함 {rate['speech_rate_syl_per_s']} 음절/초"
            )
    if "pause_count_ge_0p25" in pauses:
        med = pauses.get("juncture_median_s")
        lines.append(
            f"- 쉼: 0.25초 이상 {pauses['pause_count_ge_0p25']}회, "
            f"짧은 휴지(0.1~0.25초) {pauses['short_juncture_count_0p1_0p25']}회"
            + (f", 휴지 중앙값 {med}초" if med is not None else "")
        )
    if "half_to_half_delta_db" in energy:
        d = energy["half_to_half_delta_db"]
        trend = "후반 감소" if d < -1.0 else ("후반 증가" if d > 1.0 else "유지")
        lines.append(f"- 음량 추세: 전반 대비 후반 {d:+.2f} dB({trend}) — 상대값(AGC 영향 가능)")
    return "\n".join(lines)
