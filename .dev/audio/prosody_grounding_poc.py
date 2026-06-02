"""프로소디 그라운딩 PoC — 젬마 vocal 주장 ↔ 객관 음향 수치 대조.

차터(.dev/audio/README.md) 흐름 2단계. 비전의 vision_grounding_poc.py 대응물.
입력: kor_16k_mono.pcm (kor.mp4 → 16kHz mono Int16 PCM, 파이프라인 규격과 동일).
정답지: ../kor_gemma_out.json 의 eval[].vocal (volume/pace/pauses/intonation).

측정(전부 CPU, research-synthesis.md 권장 스택):
  - 피치/단조로움: parselmouth(Praat AC) → PDQ, F0 std(semitone), range, declination-detrend std
  - 발화속도: 강도피크(dip criterion)+유성성 게이트 음절핵(De Jong-Wempe 2009 본질) → speech/articulation rate
  - 쉼/호흡: Praat 강도 기반 silence 검출 → 쉼 횟수·길이 분포 (silero-VAD 있으면 교차검증)
  - 에너지/볼륨: librosa RMS 컨투어 → 상대 dB 추세·낙폭·최저점 ts

설계 원칙: 절대 볼륨/긴장 라벨 금지(AGC·각성≠감정 교란). 기술적 사실만 emit.
실행: ../../.venv/bin/python prosody_grounding_poc.py  (cwd=.dev/audio)
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import librosa
import numpy as np
import parselmouth
from parselmouth.praat import call
from scipy.signal import find_peaks

SR = 16000
HERE = Path(__file__).resolve().parent
PCM = HERE / "kor_16k_mono.pcm"
GEMMA = HERE.parent / "kor_gemma_out.json"

# 젬마 eval vocal 윈도우 (정답지) — ss, dur
EVAL_WINDOWS = [(0, 16), (16, 16), (21, 16)]


# ── 로드 ──
def load_pcm() -> np.ndarray:
    raw = np.frombuffer(PCM.read_bytes(), dtype=np.int16)
    return raw.astype(np.float64) / 32768.0  # [-1,1] float


def slice_sound(samples: np.ndarray, ss: float, dur: float) -> parselmouth.Sound:
    a = int(ss * SR)
    b = min(int((ss + dur) * SR), len(samples))
    return parselmouth.Sound(samples[a:b], sampling_frequency=SR)


# ── 피치 / 단조로움 (Hirst 2-pass floor/ceiling) ──
def pitch_floor_ceiling(snd: parselmouth.Sound) -> tuple[float, float]:
    """Hirst 2011 2-pass: 1차 광역 추정 → 25/75 분위로 floor/ceiling 재설정(F0 std 정확도↑)."""
    p1 = snd.to_pitch_ac(pitch_floor=60, pitch_ceiling=600)
    f = p1.selected_array["frequency"]
    f = f[f > 0]
    if f.size < 5:
        return 75.0, 500.0
    q25, q75 = np.percentile(f, [25, 75])
    return max(50.0, q25 * 0.75), min(600.0, q75 * 1.5)


def pitch_metrics(snd: parselmouth.Sound) -> dict:
    floor, ceil = pitch_floor_ceiling(snd)
    p = snd.to_pitch_ac(pitch_floor=floor, pitch_ceiling=ceil)
    t = p.xs()
    f = p.selected_array["frequency"]
    voiced = f > 0
    fv = f[voiced]
    tv = t[voiced]
    if fv.size < 5:
        return {"voiced_frames": int(fv.size), "note": "유성 프레임 부족"}
    st = 12.0 * np.log2(fv)  # 세미톤 (ref 무관 — std에 영향 없음)
    # declination detrend: 세미톤을 시간에 선형회귀 후 잔차 std (억양 생동감, 하강추세 제거)
    slope, intercept = np.polyfit(tv, st, 1)
    resid = st - (slope * tv + intercept)
    # 옥타브 오류 점검: 중앙값의 1.9배(=옥타브 근처) 초과 프레임 비율 — 높으면 SD가 인위 팽창
    median = float(np.median(fv))
    octave_up = float((fv > median * 1.9).mean())
    return {
        "voiced_frames": int(fv.size),
        "voiced_ratio": round(float(voiced.mean()), 3),
        "f0_floor_ceiling": [round(floor, 1), round(ceil, 1)],
        "mean_hz": round(float(fv.mean()), 1),
        "median_hz": round(median, 1),
        "sd_hz": round(float(fv.std()), 1),
        "pdq": round(float(fv.std() / fv.mean()), 4),  # Pitch Dynamism Quotient (Hincks)
        "sd_semitone": round(float(st.std()), 2),
        "range_st_10_90": round(float(np.percentile(st, 90) - np.percentile(st, 10)), 2),
        "declination_st_per_s": round(float(slope), 3),
        "detrended_sd_semitone": round(float(resid.std()), 2),
        "octave_error_frac": round(octave_up, 3),  # ≪0.05면 SD 신뢰 가능
    }


# ── 강도/유성성 기반 음절핵 + 쉼 (De Jong-Wempe 2009 본질) ──
def rate_and_pauses(snd: parselmouth.Sound, *, mindip=2.0, sil_off=18.0) -> dict:
    """쉼 분류: >=0.25s=진짜 쉼(운율경계), 0.1~0.25s=짧은 휴지/호흡.
    ★ sil_off=18dB: 이 녹음은 SNR~30dB(노이즈 플로어 높음)라 Praat 표준 -25dB는 잡음 바닥
      아래로 떨어져 어절 휴지를 못 잡는다. 99분위-18dB가 이 SNR의 어절경계 dip을 포착(프로브 검증)."""
    intensity = snd.to_intensity(minimum_pitch=100.0)
    it = intensity.xs()
    idb = intensity.values[0]
    dur = snd.get_total_duration()
    max99 = call(intensity, "Get quantile", 0, 0, 0.99)
    threshold = max99 - sil_off

    # 음절핵: 강도 국소피크 중 prominence(dip)>=mindip & sounding & voiced
    peaks, _ = find_peaks(idb, prominence=mindip)
    pitch = snd.to_pitch_ac()
    nuclei = 0
    for pk in peaks:
        if idb[pk] < threshold:
            continue  # 묵음 구간 피크 제외
        f0 = pitch.get_value_at_time(it[pk])
        if f0 is None or np.isnan(f0) or f0 <= 0:
            continue  # 유성성 게이트
        nuclei += 1

    # 쉼: 강도<threshold 연속 프레임 길이>=0.1s (선행/후행 묵음은 발화 외 → 제외)
    dt = it[1] - it[0] if len(it) > 1 else 0.01
    sounding_idx = np.where(idb >= threshold)[0]
    runs = []
    if sounding_idx.size:
        lo, hi = sounding_idx[0], sounding_idx[-1]  # 첫·끝 발화 프레임(내부 쉼만)
        silent = idb < threshold
        run = 0
        for i in range(lo, hi + 1):
            if silent[i]:
                run += 1
            else:
                if run * dt >= 0.1:
                    runs.append(run * dt)
                run = 0
    pauses = [r for r in runs if r >= 0.25]          # 진짜 쉼(운율경계, ~250ms norm 이상)
    junctures = [r for r in runs if 0.1 <= r < 0.25]  # 짧은 휴지/호흡
    internal_silence = float(sum(runs))
    phonation = max(dur - internal_silence, 1e-6)
    return {
        "syllable_nuclei": nuclei,
        "duration_s": round(dur, 2),
        "phonation_s": round(phonation, 2),
        "speech_rate_syl_per_s": round(nuclei / dur, 2),          # 쉼 포함
        "articulation_rate_syl_per_s": round(nuclei / phonation, 2),  # 쉼 제외
        "pause_count_ge_0p25": len(pauses),
        "short_juncture_count_0p1_0p25": len(junctures),
        "juncture_median_s": round(float(np.median(runs)), 3) if runs else None,
        "longest_pauses_s": [round(r, 2) for r in sorted(runs, reverse=True)[:5]],
    }


# ── 에너지/볼륨 컨투어 (librosa RMS, 상대값만) ──
def energy_metrics(samples: np.ndarray, ss: float, dur: float) -> dict:
    a = int(ss * SR)
    b = min(int((ss + dur) * SR), len(samples))
    y = samples[a:b].astype(np.float32)
    hop, frame = 160, 400  # 10ms hop / 25ms frame @16k (기본값은 ~22k 가정 → 명시)
    rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=hop)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=SR, hop_length=hop)
    db = librosa.amplitude_to_db(rms, ref=np.max(rms))  # 윈도우 피크=0dB 상대
    # sounding 프레임만(상위 60%): 쉼이 통계 지배하지 않게
    sounding = db > (np.percentile(db, 40))
    db_s = db[sounding]
    t_s = times[sounding]
    if db_s.size < 4:
        return {"note": "sounding 프레임 부족"}
    slope = float(np.polyfit(t_s, db_s, 1)[0])  # dB/s (음수=하락)
    half = len(db_s) // 2
    first_half = float(np.median(db_s[:half]))
    second_half = float(np.median(db_s[half:]))
    tro = int(np.argmin(db_s))  # trough도 sounding 프레임만(선행 묵음 -80dB 배제)
    return {
        "rel_db_slope_per_s": round(slope, 3),
        "first_half_median_db": round(first_half, 2),
        "second_half_median_db": round(second_half, 2),
        "half_to_half_delta_db": round(second_half - first_half, 2),  # 음수=후반 작아짐
        "sounding_trough_db": round(float(db_s[tro]), 2),  # 발화 중 최저(피크 대비)
        "sounding_trough_at_s": round(float(ss + t_s[tro]), 2),
    }


# ── 한글 음절수(전사 기반 검증용 sanity) ──
def hangul_syllables(text: str) -> int:
    return len(re.findall(r"[가-힣]", text))


def main() -> None:
    samples = load_pcm()
    gemma = json.loads(GEMMA.read_text())
    total_dur = len(samples) / SR

    # 전사 기반 전역 발화속도 sanity (음절핵 검출기 검증)
    chunks = gemma["full_transcript_chunks"]
    total_syl = sum(hangul_syllables(c["transcript"]) for c in chunks)

    out = {
        "clip": "kor.mp4 → kor_16k_mono.pcm",
        "total_duration_s": round(total_dur, 2),
        "transcript_global": {
            "hangul_syllables": total_syl,
            "speech_rate_syl_per_s_over_full_clip": round(total_syl / total_dur, 2),
            "note": "전사 한글 음절수 / 전체길이 — 음절핵 검출기 sanity check용 참조값",
        },
        "windows": [],
    }

    eval_list = gemma["eval"]
    for (ss, dur), ev in zip(EVAL_WINDOWS, eval_list):
        snd = slice_sound(samples, ss, dur)
        t0 = time.perf_counter()
        rp = rate_and_pauses(snd)
        rate_keys = ("syllable_nuclei", "duration_s", "phonation_s",
                     "speech_rate_syl_per_s", "articulation_rate_syl_per_s")
        win = {
            "window": f"{ss}-{ss + dur}s",
            "ss": ss,
            "dur": dur,
            "gemma_vocal": ev["parsed"]["vocal"],
            "pitch_intonation": pitch_metrics(snd),
            "rate": {k: rp[k] for k in rate_keys},
            "pauses": {k: v for k, v in rp.items() if k not in rate_keys},
            "energy": energy_metrics(samples, ss, dur),
        }
        win["cpu_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["windows"].append(win)

    # silero-VAD 교차검증 (있으면)
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad

        import torch

        model = load_silero_vad(onnx=True)
        sil = []
        for ss, dur in EVAL_WINDOWS:
            a, b = int(ss * SR), min(int((ss + dur) * SR), len(samples))
            seg = torch.from_numpy(samples[a:b].astype(np.float32))
            # 민감하게: 짧은 호흡까지 분리(min_silence 80ms)
            ts = get_speech_timestamps(
                seg, model, sampling_rate=SR, return_seconds=True,
                min_silence_duration_ms=80, min_speech_duration_ms=100,
            )
            gaps = [round(ts[i + 1]["start"] - ts[i]["end"], 2) for i in range(len(ts) - 1)]
            speech = sum(t["end"] - t["start"] for t in ts)
            sil.append({
                "window": f"{ss}-{ss + dur}s",
                "speech_segments": len(ts),
                "speech_ratio": round(speech / dur, 3),
                "inter_speech_gaps_s": gaps,
                "gap_median_s": round(float(np.median(gaps)), 3) if gaps else None,
            })
        out["silero_vad_crosscheck"] = sil
    except Exception as e:  # noqa
        out["silero_vad_crosscheck"] = f"미실행({type(e).__name__}: {e})"

    (HERE / "prosody_grounding_poc.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
