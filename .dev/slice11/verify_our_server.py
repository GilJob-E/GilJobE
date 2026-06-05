#!/usr/bin/env python3
"""Slice 12 라이브 검증 — **우리 HTTP 서버**(python -m giljobe.server)를 스택에 붙여 전 루프 검증.

Slice 11에서 그쪽 자작 wrapper가 in-container subscriber로 전사를 못 냈다(빈 문자열, 발행 중
records=0, Event loop is closed). 우리 서버는 단일 영속 aiohttp 루프 위에서 구독자를 돌려 그 버그를
구조적으로 없앤다. 여기서 **우리 서버가 같은 스택(livekit 7880·공유 gemma 8000)에서 브라우저 publish를
계약(/subscriber/start|stop·/signals)으로 전사**하는지 종단 검증한다.

흐름:
  1) 우리 서버를 host 8201로 기동(env: LIVEKIT_URL=ws://localhost:7880, 키=api 컨테이너서 읽음,
     VLLM_BASE_URL=http://localhost:8000). /healthz·/readyz 대기.
  2) httpd:8899(pub.html) + drive_browser_pub.py(가짜장치 헤드리스 Chrome)로 같은 룸 publish.
  3) POST /subscriber/start → 우리 서버가 룸 join. 발행 동안 GET /signals 폴링
     (★ 발행 중 transcript 채워짐 = 그쪽 wrapper가 못한 것).
  4) media 살아있을 때 POST /subscriber/stop → turn_end. 최종 /signals → transcriptFull.

env: SESSION_ID(기본 s12srv) · PUB_HOLD_S(기본 42) · OUR_PORT(8201)
실행: PYTHONPATH=src .venv/bin/python .dev/slice11/verify_our_server.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path("/home/kio/workspace/giljobe")
SLICE10 = REPO / ".dev" / "slice10"
HERE = REPO / ".dev" / "slice11"
SESSION_ID = os.environ.get("SESSION_ID", "s12srv")
PUB_HOLD_S = float(os.environ.get("PUB_HOLD_S", "42"))
OUR_PORT = int(os.environ.get("OUR_PORT", "8201"))
OUR = f"http://localhost:{OUR_PORT}"
EVID = HERE / "our-server-live.json"


def _read_container_keys() -> tuple[str, str]:
    """api 컨테이너 env에서 LiveKit 키를 읽어 self-mint에 쓴다(smoke 하먼스와 동일 — 값 미출력)."""
    out = subprocess.run(
        ["docker", "inspect", "giljob-v2-api-1",
         "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
        capture_output=True, text=True, timeout=10,
    )
    env = dict(ln.split("=", 1) for ln in out.stdout.splitlines() if "=" in ln)
    return env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode()[:300]}


def _summ(sig: dict) -> str:
    recs = sig.get("records", [])
    wt = [r.get("transcript", "") for r in recs if r.get("type") == "window"]
    nonempty = sum(1 for t in wt if t.strip())
    tf = (sig.get("transcriptFull") or "").strip()
    return f"records={len(recs)} win_tx={nonempty}/{len(wt)} 채움 transcriptFull[{len(tf)}자]"


def _start_server() -> "subprocess.Popen[bytes]":
    key, secret = _read_container_keys()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": "src",
        "LIVEKIT_URL": "ws://localhost:7880",
        "LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret,
        "VLLM_BASE_URL": "http://localhost:8000",
        "ANALYSIS_ENGINE_PORT": str(OUR_PORT),
    })
    log = open(HERE / "our-server.log", "w")  # noqa: SIM115
    return subprocess.Popen([".venv/bin/python", "-u", "-m", "giljobe.server"],
                            cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)


def _wait_ready(timeout_s: float = 60.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            if _get(f"{OUR}/healthz").get("status") == "ok":
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def _start_httpd() -> "subprocess.Popen[bytes]":
    return subprocess.Popen(["python3", "-m", "http.server", "8899", "--bind", "127.0.0.1"],
                            cwd=str(SLICE10), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _launch_pub() -> "subprocess.Popen[bytes]":
    env = dict(os.environ)
    env.update({"GILJOBE_SMOKE_ROOM_ID": SESSION_ID, "PUB_HOLD_S": str(PUB_HOLD_S), "PUB_PORT": "8899"})
    return subprocess.Popen([".venv/bin/python", "-u", str(SLICE10 / "drive_browser_pub.py")],
                            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main() -> int:
    print(f"[s12] 우리 서버 기동 :{OUR_PORT} (SESSION_ID={SESSION_ID})")
    server = _start_server()
    httpd = _start_httpd()
    snaps: list[dict] = []
    try:
        if not _wait_ready():
            print("[s12] ERROR: 서버 /healthz 미응답")
            return 2
        ready = _get(f"{OUR}/readyz")
        print(f"[s12] readyz={ready}")

        code, sresp = _post(f"{OUR}/subscriber/start", {"sessionId": SESSION_ID, "criticMode": "window"})
        print(f"[s12] start → {code} {sresp}")

        pub = _launch_pub()
        print(f"[s12] publisher pid={pub.pid}")

        t0 = time.monotonic()
        posted_stop = False
        stop_resp: dict = {}
        while time.monotonic() - t0 < PUB_HOLD_S + 8:
            time.sleep(5.0)
            try:
                sig = _get(f"{OUR}/signals?sessionId={SESSION_ID}")
            except Exception as e:  # noqa: BLE001
                print(f"[s12] signals 폴 실패: {e}")
                continue
            el = time.monotonic() - t0
            print(f"[s12] +{el:4.1f}s  {_summ(sig)}")
            snaps.append({"elapsed": round(el, 1), "summary": _summ(sig),
                          "transcriptFull": sig.get("transcriptFull")})
            if not posted_stop and el >= PUB_HOLD_S - 8:
                code, stop_resp = _post(f"{OUR}/subscriber/stop", {})
                posted_stop = True
                print(f"[s12] stop(flush) → {code} {stop_resp}")
        if not posted_stop:
            _, stop_resp = _post(f"{OUR}/subscriber/stop", {})

        time.sleep(2.0)
        final = _get(f"{OUR}/signals?sessionId={SESSION_ID}")
        print(f"[s12] FINAL  {_summ(final)}")
        tf = (final.get("transcriptFull") or "").strip()
        print(f"[s12] transcriptFull = {tf[:120]!r}")

        try:
            pub_tail = (pub.communicate(timeout=10)[0] or b"").decode(errors="replace")[-800:]
        except Exception:  # noqa: BLE001
            pub.kill(); pub_tail = ""
        EVID.write_text(json.dumps({
            "slice": 12, "check": "our server (python -m giljobe.server) live full loop",
            "sessionId": SESSION_ID, "snapshots": snaps, "stopResp": stop_resp,
            "final_summary": _summ(final), "transcriptFull": final.get("transcriptFull"),
            "windowTranscripts": final.get("windowTranscripts"),
            "latestTurnEnd": final.get("latestTurnEnd"),
            "records": final.get("records"), "pubTail": pub_tail,
        }, ensure_ascii=False, indent=2))
        print(f"[s12] evidence → {EVID}")
        return 0 if tf else 4
    finally:
        httpd.terminate()
        server.terminate()
        try:
            server.wait(timeout=8)
        except Exception:  # noqa: BLE001
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
