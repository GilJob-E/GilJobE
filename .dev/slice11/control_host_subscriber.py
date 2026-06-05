#!/usr/bin/env python3
"""Slice 11 대조군 — **우리 host subscriber**(build_subscriber 동형, slice10 하먼스)로 같은 브라우저
publish를 전사해, '전사 빔' 버그가 우리 코드(b769120)인지 그쪽 wrapper인지 격리한다.

논리: 그쪽 in-container subscriber는 nv(영상)는 내는데 transcript(오디오)가 전부 빈 문자열이었다.
critic.py는 transcriber를 자동 기본생성하므로(WindowCritic(client) → GemmaNativeTranscriber 자동),
전사 빔은 (a) audio PCM이 stt 레인에 안 먹힘 또는 (b) 이 경로 전사가 빈 반환이다. 여기서 **현재 스택
그대로**(node_ip=Tailscale, 공유 gemma-e4b 8000) 우리 host subscriber를 같은 룸에 붙여 같은 publish를
전사시킨다:
  - 전사가 채워지면 → 우리 코드·백엔드·audio/red·현재 config 전부 정상 → 버그는 **그쪽 wrapper 경로** 귀속.
  - 전사가 비면 → 환경/우리코드 회귀 → 우리 쪽 조사 필요.

흐름: smoke_browser_live.py(subscriber, sentinel 대기) 백그라운드 → 접속 갭 후 drive_browser_pub.py
(publisher, PUB_HOLD_S 발행 후 sentinel touch) → subscriber flush·evidence → 요약 출력.

실행: PYTHONPATH=src .venv/bin/python .dev/slice11/control_host_subscriber.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path("/home/kio/workspace/giljobe")
SLICE10 = REPO / ".dev" / "slice10"
ROOM_ID = os.environ.get("ROOM_ID", "s11ctrl")
PUB_HOLD_S = os.environ.get("PUB_HOLD_S", "35")
SENTINEL = Path("/tmp/giljobe_smoke_stop")
EVIDENCE = REPO / "tests" / "evidence" / "browser-live.raw.json"


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["GILJOBE_SMOKE_ROOM_ID"] = ROOM_ID
    env.update(extra)
    return env


def main() -> int:
    SENTINEL.unlink(missing_ok=True)
    print(f"[ctrl] room=giljob-session-{ROOM_ID}  (host subscriber 대조군)")

    # 0) pub.html + lk-sdk.mjs(2.7.5)를 8899로 정적 서빙(publisher가 로드).
    httpd = subprocess.Popen(
        ["python3", "-m", "http.server", "8899", "--bind", "127.0.0.1"],
        cwd=str(SLICE10), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    # 1) 우리 subscriber(slice10 하먼스)를 백그라운드로 — 접속 후 sentinel까지 대기.
    sub = subprocess.Popen(
        [".venv/bin/python", "-u", str(SLICE10 / "smoke_browser_live.py")],
        cwd=str(REPO), env=_env(GILJOBE_SMOKE_MODE="live", GILJOBE_SMOKE_MAX_S="120"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # 접속·warmup 갭(콜드 멀티모달 그래프 워밍 포함) — 넉넉히.
    print("[ctrl] subscriber 기동 — 접속/warmup 25s 대기…")
    time.sleep(25.0)

    # 2) publisher 구동(같은 룸). 내부에서 PUB_HOLD_S 발행 후 sentinel touch.
    pub = subprocess.Popen(
        [".venv/bin/python", "-u", str(SLICE10 / "drive_browser_pub.py")],
        cwd=str(REPO), env=_env(PUB_HOLD_S=PUB_HOLD_S),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    pub_out = pub.communicate(timeout=160)[0].decode(errors="replace")
    print("[ctrl] --- publisher tail ---")
    print("\n".join(pub_out.splitlines()[-8:]))

    # 3) subscriber가 sentinel 보고 flush·종료할 때까지.
    try:
        sub_out = sub.communicate(timeout=60)[0].decode(errors="replace")
    except subprocess.TimeoutExpired:
        sub.kill()
        sub_out = sub.communicate()[0].decode(errors="replace")
    print("[ctrl] --- subscriber tail ---")
    print("\n".join(sub_out.splitlines()[-20:]))

    # 4) evidence에서 전사 판정.
    if EVIDENCE.exists():
        ev = json.loads(EVIDENCE.read_text())
        tf = ev.get("turn_end", {}).get("transcript_full", "")
        wins = ev.get("windows", [])
        win_tx = [w.get("transcript", "") for w in wins]
        nonempty = [t for t in win_tx if t.strip()]
        print(f"\n[ctrl] ★판정 transcript_full[{len(tf)}자]={tf[:80]!r}")
        print(f"[ctrl] window transcripts: {len(nonempty)}/{len(win_tx)} 비지 않음")
        print(f"[ctrl] counts={ev.get('counts')}  remote={ev.get('remote_participants')}")
        httpd.terminate()
        return 0 if (tf.strip() or nonempty) else 5
    print("[ctrl] evidence 없음")
    httpd.terminate()
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
