#!/usr/bin/env python3
"""★ Slice 10 사람-스모크 하먼스 — **실 브라우저**(getUserMedia→livekit publish) → 우리 subscriber.

Slice 9까지는 합성 publisher(우리가 kor.mp4를 직접 LiveKit에 push)였다. 여기서는 **진짜 브라우저**가
통합 스택 프론트(caddy:80 `/interviews/{id}/room`)에서 웹캠+마이크로 publish하고, 우리 LiveKitSubscriber
(실 WindowCritic + GemmaNativeTranscriber)가 같은 룸에 hidden으로 붙어 실 발화를 전사·분석한다.

경로 = Slice 10 결정 A(독립): 그쪽 analysis-engine(`ANALYSIS_ENGINE_ENABLE_SUBSCRIBER=false`)과 무관하게
우리 subscriber를 **로컬에서** 띄워 단독 검증. 룸 코디네이션: api가 요청 sessionId를 존중하므로 우리가
룸 id를 미리 정하고(`giljob-session-{ROOM_ID}`) 사용자가 같은 id의 URL로 들어오면 같은 룸에서 만난다.

src 무수정 — 계측은 테스트 레벨(Slice 9 `_ClockSink`/`_InstWindower`/`_resolve_creds` 재사용).

사람-스모크라 종료 시점이 사람-페이스(웹캠 발화)다. 고정 media_dur 대신:
  - **sentinel 파일**(STOP_FILE)이 나타나면 종료(운영자가 `touch`로 신호), 또는
  - MAX_DURATION 초과 시 종료(backstop).
종료 시 aclose(end_turn flush) → evidence json + 콘솔 요약.

모드(env GILJOBE_SMOKE_MODE):
  - check: MockCritic로 룸에 빠르게 붙어 원격 참가자/트랙만 보고하고 종료(creds·attach 검증, vLLM 불요).
  - live : 실 WindowCritic로 종단 캡처(기본).

env:
  GILJOBE_SMOKE_ROOM_ID  룸 식별자(기본 kio-smoke-10) → 룸 `giljob-session-{id}`, URL `/interviews/{id}/room`
  GILJOBE_SMOKE_MODE     check|live (기본 live)
  GILJOBE_SMOKE_STOP     sentinel 경로(기본 /tmp/giljobe_smoke_stop)
  GILJOBE_SMOKE_MAX_S    backstop 초(기본 600)
  LIVEKIT_API_KEY/SECRET env 키 우선(없으면 컨테이너 env→dev fallback; `_resolve_creds`)

실행: PYTHONPATH=src .venv/bin/python .dev/slice10/smoke_browser_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

ROOM_ID = os.environ.get("GILJOBE_SMOKE_ROOM_ID", "kio-smoke-10")
ROOM = f"giljob-session-{ROOM_ID}"
MODE = os.environ.get("GILJOBE_SMOKE_MODE", "live")
STOP_FILE = Path(os.environ.get("GILJOBE_SMOKE_STOP", "/tmp/giljobe_smoke_stop"))
MAX_S = float(os.environ.get("GILJOBE_SMOKE_MAX_S", "600"))

LIVEKIT_HOST_URL = "ws://localhost:7880"
DEV_KEY, DEV_SECRET = "devkey", "secret"  # 독립 livekit-dev fallback(통합 키 아님)
POLL_INTERVAL = 0.5
EOT_DEADLINE = 8.0

REPO = Path(__file__).resolve().parents[2]
JSONL_PATH = REPO / ".dev" / "slice10" / "smoke-signals.jsonl"
EVIDENCE = REPO / "tests" / "evidence" / "browser-live.raw.json"


# ── LiveKit 자격증명 해석(Slice 9 _resolve_creds와 동일 정책; 값 미출력) ──
def _read_container_keys() -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", "giljob-v2-api-1",
             "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        env = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        key, secret = env.get("LIVEKIT_API_KEY"), env.get("LIVEKIT_API_SECRET")
        return (key, secret) if key and secret else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_creds() -> tuple[str, str, str, str] | None:
    key, secret = os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    url = os.environ.get("LIVEKIT_URL") or LIVEKIT_HOST_URL
    source = "env"
    if not (key and secret):
        ks = _read_container_keys()
        if ks is not None:
            key, secret = ks
            url = LIVEKIT_HOST_URL
            source = "container(giljob-v2)"
        else:
            key, secret = DEV_KEY, DEV_SECRET
            source = "dev-fallback"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname or "localhost", int(port or 7880)), timeout=2):
            pass
    except OSError:
        return None
    return url, key, secret, source


# ── 계측(src 무수정) — Slice 9와 동일 ──
class _ClockSink:
    def __init__(self, clock) -> None:  # noqa: ANN001 - loop.time 콜러블
        self.clock = clock
        self.entries: list[tuple[float, dict[str, object]]] = []

    def emit(self, record: dict[str, object]) -> None:
        self.entries.append((self.clock(), record))


class _InstWindower:
    def __init__(self, inner, clock) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "first_video_t", None)
        object.__setattr__(self, "first_audio_t", None)

    def add_frame(self, t_s: float, jpeg: bytes) -> None:
        if self.first_video_t is None:
            object.__setattr__(self, "first_video_t", self._clock())
        self._inner.add_frame(t_s, jpeg)

    def add_audio(self, t_s: float, pcm: bytes) -> None:
        if self.first_audio_t is None:
            object.__setattr__(self, "first_audio_t", self._clock())
        self._inner.add_audio(t_s, pcm)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


async def _wait_until_stop(loop_clock, started_at: float) -> str:
    """sentinel 파일이 나타나거나 MAX_S 초과까지 대기. 종료 사유 문자열 반환."""
    if STOP_FILE.exists():
        STOP_FILE.unlink()  # 이전 잔여 제거(시작 시 깨끗이)
    while True:
        await asyncio.sleep(0.5)
        if STOP_FILE.exists():
            return "sentinel"
        if loop_clock() - started_at > MAX_S:
            return "max_duration"


def _build_critic(mode: str):
    if mode == "check":
        from giljobe.analysis.mocks import MockCritic
        return MockCritic(), None
    from giljobe.analysis.critic import WindowCritic
    from giljobe.llm.vllm_client import default_vllm_client
    client = default_vllm_client()
    client.health()  # 다운이면 예외 → 호출자가 중단
    return WindowCritic(client=client), client


async def run() -> None:
    from livekit import rtc

    from giljobe.emit.sink import JSONLSink, SignalRecorder
    from giljobe.pipeline.windowing import TurnWindower
    from giljobe.server.subscriber import LiveKitSubscriber
    from giljobe.server.token import subscriber_token

    creds = _resolve_creds()
    if creds is None:
        raise SystemExit(f"LiveKit 미도달({LIVEKIT_HOST_URL}) — 스택 livekit 기동 확인")
    lk_url, lk_key, lk_secret, lk_src = creds
    print(f"[smoke] room={ROOM}  livekit={lk_url} (keys: {lk_src})  mode={MODE}")
    print(f"[smoke] ★ 브라우저에서 열 URL: http://localhost/interviews/{ROOM_ID}/room")

    critic, client = _build_critic(MODE)
    if client is not None:
        print(f"[smoke] vLLM={client.base_url} — warmup 중(콜드 그래프)...")

    loop = asyncio.get_running_loop()
    if MODE == "live":
        await loop.run_in_executor(None, critic.warmup)

    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    jsonl = JSONLSink(JSONL_PATH)
    clock = _ClockSink(loop.time)
    recorder = SignalRecorder(ROOM_ID, [jsonl, clock])
    windower = TurnWindower(critic, eot_deadline_s=EOT_DEADLINE)
    inst = _InstWindower(windower, loop.time)
    sub = LiveKitSubscriber(rtc.Room(), inst, recorder, poll_interval=POLL_INTERVAL)
    sub_token = subscriber_token(lk_key, lk_secret, ROOM)

    state: dict[str, Any] = {}
    await sub.start()
    await asyncio.wait_for(sub.connect(lk_url, sub_token), timeout=15)
    print(f"[smoke] 룸 접속 완료. 원격 참가자 {len(sub.room.remote_participants)}명. "
          f"{'발화하세요. ' if MODE=='live' else ''}종료: touch {STOP_FILE}")

    started = loop.time()
    reason = "error"
    try:
        if MODE == "check":
            await asyncio.sleep(6.0)  # 잠깐 붙어 참가자/트랙 관찰
            reason = "check"
        else:
            reason = await _wait_until_stop(loop.time, started)
    finally:
        state["reason"] = reason
        state["aclose_at"] = loop.time()
        await sub.aclose()
        state["t0"] = sub._t0
        state["first_video_t"] = inst.first_video_t
        state["first_audio_t"] = inst.first_audio_t
        state["clock_entries"] = clock.entries
        state["remote"] = {
            p.identity: [getattr(pub, "kind", "?") for pub in p.track_publications.values()]
            for p in sub.room.remote_participants.values()
        }
    jsonl.close()

    _report(state, lk_url, lk_src, client)


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return {"n": len(xs), "min": round(min(xs), 3),
            "median": round(statistics.median(xs), 3), "max": round(max(xs), 3)}


def _report(state: dict[str, Any], lk_url: str, lk_src: str, client) -> None:  # noqa: ANN001
    parsed = [json.loads(line) for line in JSONL_PATH.read_text(encoding="utf-8").splitlines()] \
        if JSONL_PATH.exists() else []
    types = Counter(r["type"] for r in parsed)
    windows = [r for r in parsed if r["type"] == "window"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]

    t0, aclose_at = state.get("t0"), state.get("aclose_at")
    live_lat, eot_lat, win_rows = [], [], []
    for (te, r) in state.get("clock_entries", []):
        if r["type"] != "window":
            continue
        media_end = r["t"] + r["window_s"] / 2.0
        lag = None if t0 is None else round(te - (t0 + media_end), 3)
        is_eot = aclose_at is not None and te >= aclose_at
        if lag is not None:
            (eot_lat if is_eot else live_lat).append(lag)
        win_rows.append({"t": r["t"], "window_s": r["window_s"], "nonverbal": r["nonverbal"],
                         "transcript": r["transcript"], "transcript_final": r["transcript_final"],
                         "freshness_lag_s": lag, "flushed_at_eot": is_eot})

    fv, fa = state.get("first_video_t"), state.get("first_audio_t")
    crosstrack = {
        "first_video_after_t0_s": None if (fv is None or t0 is None) else round(fv - t0, 3),
        "first_audio_after_t0_s": None if (fa is None or t0 is None) else round(fa - t0, 3),
        "skew_audio_minus_video_s": None if (fv is None or fa is None) else round(fa - fv, 3),
        "note": ("실 브라우저: 카메라/마이크 토글이 별개 UI 액션이라 첫 도착 skew는 디바이스-동기 skew가"
                 " 아니라 사용자 토글 간격을 반영할 수 있음. 양 트랙 라이브 후 per-window nv+transcript"
                 " 페어링이 실측 관심사."),
    }

    transcript_full = turn_ends[0]["transcript_full"] if turn_ends else ""
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps({
        "slice": 10,
        "check": "browser-live smoke (real browser getUserMedia→livekit publish → our subscriber)",
        "mode": MODE,
        "room": ROOM,
        "browser_url": f"http://localhost/interviews/{ROOM_ID}/room",
        "transport": f"livekit @{lk_url} (keys: {lk_src})",
        "vllm_base_url": getattr(client, "base_url", None),
        "stop_reason": state.get("reason"),
        "remote_participants": state.get("remote"),
        "counts": dict(types),
        "latency_freshness_lag": {"live_windows": _stats(live_lat),
                                  "eot_flushed_windows": _stats(eot_lat)},
        "crosstrack_t0": crosstrack,
        "windows": win_rows,
        "turn_end": {"transcript_full": transcript_full,
                     "transcript_full_hangul": _has_hangul(transcript_full),
                     "eval": turn_ends[0]["eval"] if turn_ends else None},
        "verdict": "사람이 .json으로 큐레이션(루브릭 판정)",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== Slice 10 browser-live smoke ===  stop={state.get('reason')}  counts={dict(types)}")
    print(f"remote participants={state.get('remote')}")
    print(f"freshness lag(live)={_stats(live_lat)}  crosstrack_skew={crosstrack['skew_audio_minus_video_s']}s")
    for w in win_rows:
        nv = w["nonverbal"] or {}
        print(f"  t={w['t']:>5} nv={nv.get('state','-')}/{nv.get('intensity','-')} "
              f"lag={w['freshness_lag_s']}s tx={w['transcript']!r}")
    print(f"  transcript_full({len(transcript_full)}자)={transcript_full!r}")
    if turn_ends:
        print(f"  turn_end.eval={turn_ends[0]['eval']}")
    print(f"[smoke] evidence → {EVIDENCE}")


if __name__ == "__main__":
    asyncio.run(run())
