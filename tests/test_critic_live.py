"""★ Slice 3 라이브 검증 — 실 vLLM에서 `WindowCritic`이 동작하는가?

이 슬라이스의 핵심 산출물 두 가지를 실 E4B로 end-to-end 확인한다:
  ① `read_nonverbal` (채널①, 핵심 포팅) — 영상+음성 윈도우 → NonVerbalSignal,
     state가 유효 라벨 집합 안, intensity 0–1, 윈도우 좌표 echo.
  ② `transcribe_window` (신규) — 주입된 GemmaNativeTranscriber로의 위임이 critic을 통해
     실제로 한국어 전사를 내는지(빈출력/번역 아님). Slice 2 스파이크는 transcriber 단독을
     봤고, 여기선 **critic 경유 위임**을 본다.
부수로 `evaluate_window(compact=True)`(채널②, 부차)도 한 번 찍어 포팅이 깨지지 않았는지 본다.

실 vLLM 의존 → `client.health()` 게이트로 다운이면 skip(gje `test_e2e_pipeline` 관례).
측정치는 `tests/evidence/critic-live.raw.json`에 기록(판정은 사람이 큐레이션하는 .json).
재현: vLLM 기동 후 `.venv/bin/python -m pytest tests/test_critic_live.py -v -s`
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from giljobe.analysis.critic import WindowCritic
from giljobe.llm.vllm_client import default_vllm_client
from giljobe.models.signals import NONVERBAL_STATES

# 실측 한국어 클립(테스트 픽스처 — repo 루트, gitignore라 미커밋). 없으면 skip.
CLIP = Path(__file__).parent.parent / "kor.mp4"
SAMPLE_RATE = 16000
# nv read + 전사용 3s 윈도우 두 개(중반/기술 구간), 그리고 compact eval용 한 개.
NV_WINDOWS = [(10.0, 3.0), (20.0, 3.0)]
EVAL_WINDOW = (10.0, 6.0)
EVIDENCE = Path(__file__).parent / "evidence" / "critic-live.raw.json"


def _extract_pcm(clip: Path, *, ss: float, dur: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    """[ss, ss+dur) 구간 → 16k mono Int16 PCM(라이브 ingest 경로와 동일 리샘플)."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(clip),
        "-vn", "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg pcm extract failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def _extract_jpeg_frames(clip: Path, *, ss: float, dur: float, fps: float = 1.0) -> list[bytes]:
    """[ss, ss+dur) 구간 → 1fps JPEG 프레임 바이트 리스트(라이브 ingest의 1fps throttle과 동일).
    temp 디렉터리에 번호 프레임으로 뽑아 순서대로 읽는다(assemble_video_url 입력 규격)."""
    with tempfile.TemporaryDirectory() as d:
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(clip),
            "-vf", f"fps={fps:g}", "-q:v", "3", str(Path(d, "f%05d.jpg")),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg jpeg extract failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
        return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"))]


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def test_critic_live():
    client = default_vllm_client()
    try:
        client.health()
    except Exception as e:  # noqa: BLE001 - 다운이면 라이브 검증 불가 → skip
        pytest.skip(f"vLLM down ({client.base_url}): {e}")

    if not CLIP.exists():
        pytest.skip(f"clip missing: {CLIP}")

    critic = WindowCritic(client=client)  # 기본 transcriber = GemmaNative(같은 client 공유)
    nv_rows: list[dict] = []

    for ss, dur in NV_WINDOWS:
        frames = _extract_jpeg_frames(CLIP, ss=ss, dur=dur)
        pcm = _extract_pcm(CLIP, ss=ss, dur=dur)
        assert frames, f"no frames extracted at {ss}s"

        t0 = time.perf_counter()
        nv = critic.read_nonverbal(frames, pcm, t=ss + dur / 2, window_s=dur)
        nv_latency = time.perf_counter() - t0

        t0 = time.perf_counter()
        transcript = critic.transcribe_window(pcm, t=ss + dur / 2, window_s=dur)
        stt_latency = time.perf_counter() - t0

        row = {
            "ss": ss, "dur": dur, "frames": len(frames), "pcm_bytes": len(pcm),
            "nv": nv.to_dict(), "nv_latency_s": round(nv_latency, 3),
            "transcript": transcript, "stt_latency_s": round(stt_latency, 3),
            "transcript_chars": len(transcript), "transcript_hangul": _has_hangul(transcript),
        }
        nv_rows.append(row)
        print(f"\n[{ss:g}-{ss + dur:g}s] nv={nv.state}/{nv.intensity:.2f} ({nv_latency:.2f}s) "
              f"note={nv.note!r}\n  stt({stt_latency:.2f}s, {len(transcript)}자)={transcript!r}")

    # 채널② compact eval 한 번(부차 — 포팅이 깨지지 않았는지만 확인)
    ev_ss, ev_dur = EVAL_WINDOW
    ev_frames = _extract_jpeg_frames(CLIP, ss=ev_ss, dur=ev_dur)
    ev_pcm = _extract_pcm(CLIP, ss=ev_ss, dur=ev_dur)
    t0 = time.perf_counter()
    ev = critic.evaluate_window(
        ev_frames, ev_pcm, window_start_s=ev_ss, window_dur_s=ev_dur, compact=True
    )
    ev_latency = time.perf_counter() - t0
    print(f"\n[eval compact {ev_ss:g}-{ev_ss + ev_dur:g}s | {ev_latency:.2f}s] "
          f"summary={ev.key_observations} critique={ev.critique}")

    # 측정치 먼저 기록(assert 실패해도 증거 보존). 판정은 사람이 .json으로 큐레이션.
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "slice": 3, "check": "critic-live", "model": critic.model,
                "sample_rate": SAMPLE_RATE, "clip": CLIP.name,
                "nv_windows": nv_rows,
                "eval_compact": {
                    "ss": ev_ss, "dur": ev_dur, "latency_s": round(ev_latency, 3),
                    "compact": ev.compact,
                    "key_observations": ev.key_observations, "critique": ev.critique,
                },
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # ① 채널① 비언어: 유효 라벨·범위·윈도우 좌표 echo
    for r in nv_rows:
        assert r["nv"]["state"] in NONVERBAL_STATES, f"invalid state: {r['nv']}"
        assert 0.0 <= r["nv"]["intensity"] <= 1.0, f"intensity OOR: {r['nv']}"
        assert r["nv"]["window_s"] == r["dur"], "window_s not echoed"
    # ② 신규 transcribe_window 위임: 음성 윈도우는 한글 전사를 낸다(빈출력/번역 아님)
    for r in nv_rows:
        assert r["transcript_chars"] > 0, f"empty transcript: {r['ss']}s"
        assert r["transcript_hangul"], f"no Hangul (translation/refusal?): {r['ss']}s → {r['transcript']!r}"
    # 채널② compact eval: EvaluationSignal compact 모양(포팅 무결성)
    assert ev.compact is True
