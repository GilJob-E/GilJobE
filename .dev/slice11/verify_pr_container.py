#!/usr/bin/env python3
"""Slice 13 — **PR 컨테이너**(giljob-docker analysis-engine = 우리 서버) in-container 전 루프 검증.

Slice 11에서 그쪽 자작 wrapper는 in-container subscriber로 전사를 못 냈다(빈 문자열). 이 테스트는
**PR이 빌드한 컨테이너**(python -m giljobe.server, 스택 네트워크에서 내부 livekit·gemma 사용)가 같은
환경에서 전사를 내는지 본다 — PR의 결정적 증명.

전제: PR 이미지가 ae-pr-test로 스택 네트워크에 떠 있고 host 8202로 노출(:8200). 이 스크립트는 그쪽
컨테이너를 안 만지고, fake-device 헤드리스 Chrome으로 같은 livekit 룸에 publish → 계약 구동.

흐름: POST 8202/subscriber/start → drive_browser_pub.py(가짜장치) publish → GET 8202/signals 폴링
(★ in-container 전사 채워짐) → media 살아있을 때 stop → turn_end.transcript_full.

env: SESSION_ID(기본 prtest) · PUB_HOLD_S(기본 42)
실행: PYTHONPATH=src .venv/bin/python .dev/slice11/verify_pr_container.py
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
SESSION_ID = os.environ.get("SESSION_ID", "prtest")
PUB_HOLD_S = float(os.environ.get("PUB_HOLD_S", "42"))
OUR = "http://localhost:8202"   # PR 컨테이너(ae-pr-test) host 노출
EVID = HERE / "pr-container-live.json"


def _get(u: str) -> dict[str, Any]:
    with urllib.request.urlopen(u, timeout=15) as r:
        return json.loads(r.read())


def _post(u: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(u, data=json.dumps(body).encode(),
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


def _httpd() -> "subprocess.Popen[bytes]":
    return subprocess.Popen(["python3", "-m", "http.server", "8899", "--bind", "127.0.0.1"],
                            cwd=str(SLICE10), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _pub() -> "subprocess.Popen[bytes]":
    env = dict(os.environ)
    env.update({"GILJOBE_SMOKE_ROOM_ID": SESSION_ID, "PUB_HOLD_S": str(PUB_HOLD_S), "PUB_PORT": "8899"})
    return subprocess.Popen([".venv/bin/python", "-u", str(SLICE10 / "drive_browser_pub.py")],
                            cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def main() -> int:
    print(f"[pr] PR 컨테이너 in-container 검증 SESSION_ID={SESSION_ID}")
    print(f"[pr] readyz={_get(f'{OUR}/readyz')}")
    httpd = _httpd()
    time.sleep(0.5)
    snaps: list[dict] = []
    try:
        code, sresp = _post(f"{OUR}/subscriber/start", {"sessionId": SESSION_ID, "criticMode": "window"})
        print(f"[pr] start → {code} {sresp}")
        pub = _pub()
        print(f"[pr] publisher pid={pub.pid}")
        t0 = time.monotonic()
        posted_stop = False
        stop_resp: dict = {}
        while time.monotonic() - t0 < PUB_HOLD_S + 8:
            time.sleep(5.0)
            try:
                sig = _get(f"{OUR}/signals?sessionId={SESSION_ID}")
            except Exception as e:  # noqa: BLE001
                print(f"[pr] 폴 실패: {e}"); continue
            el = time.monotonic() - t0
            print(f"[pr] +{el:4.1f}s  {_summ(sig)}")
            snaps.append({"elapsed": round(el, 1), "summary": _summ(sig)})
            if not posted_stop and el >= PUB_HOLD_S - 8:
                code, stop_resp = _post(f"{OUR}/subscriber/stop", {})
                posted_stop = True
                print(f"[pr] stop → {code} {stop_resp}")
        if not posted_stop:
            _, stop_resp = _post(f"{OUR}/subscriber/stop", {})
        time.sleep(2.0)
        final = _get(f"{OUR}/signals?sessionId={SESSION_ID}")
        print(f"[pr] FINAL {_summ(final)}")
        tf = (final.get("transcriptFull") or "").strip()
        print(f"[pr] transcriptFull = {tf[:140]!r}")
        try:
            pub_tail = (pub.communicate(timeout=10)[0] or b"").decode(errors="replace")[-500:]
        except Exception:  # noqa: BLE001
            pub.kill(); pub_tail = ""
        EVID.write_text(json.dumps({
            "slice": 13, "check": "PR container (giljob-docker analysis-engine) in-container full loop",
            "sessionId": SESSION_ID, "snapshots": snaps, "stopResp": stop_resp,
            "final_summary": _summ(final), "transcriptFull": final.get("transcriptFull"),
            "latestTurnEnd": final.get("latestTurnEnd"), "records": final.get("records"),
            "pubTail": pub_tail,
        }, ensure_ascii=False, indent=2))
        print(f"[pr] evidence → {EVID}")
        return 0 if tf else 4
    finally:
        httpd.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
