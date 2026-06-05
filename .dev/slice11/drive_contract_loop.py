#!/usr/bin/env python3
"""Slice 11 — 그쪽 analysis-engine **HTTP 계약**으로 in-container subscriber를 구동해 전 루프 검증.

Slice 9/10은 우리가 직접 subscriber를 띄웠다(harness). Slice 11의 새 검증점은 **그쪽 wrapper의
계약**(`/analysis/subscriber/start|stop`, `/analysis/signals`)을 그대로 써서, 그쪽 컨테이너 안에서
도는 우리 코드(핀 b769120)가 실제로 룸에 join→media 수신→signal emit 하는지다.

흐름:
  1) POST /analysis/subscriber/start {sessionId, criticMode:"window"} → 그쪽이 룸 giljob-session-{id}에 subscriber attach.
  2) drive_browser_pub.py(가짜장치 헤드리스 Chrome)로 같은 룸에 candidate publish(audio+video).
  3) 발행 동안 GET /analysis/signals?sessionId= 폴링 → window/eval 레코드가 채워지는지.
  4) media 살아있는 동안 POST /analysis/subscriber/stop → 턴 flush(end_turn) → turn_end.transcript_full 확인.
  5) evidence(JSON) + 요약 출력.

핵심 변수: livekit node_ip=Tailscale 잔여 상태에서 **컨테이너 내부 subscriber가 media를 받는가**(Slice 10
host subscriber와 다른 경로). signals가 비면 in-container media 도달 실패 = node_ip 블로커 실증.

env: SESSION_ID(기본 slice11run) · PUB_HOLD_S(기본 42) · CADDY(기본 http://localhost) · PUB_PORT(8899)
실행: PYTHONPATH=src .venv/bin/python .dev/slice11/drive_contract_loop.py
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

HERE = Path(__file__).resolve().parent
SLICE10 = HERE.parent / "slice10"
SESSION_ID = os.environ.get("SESSION_ID", "slice11run")
PUB_HOLD_S = float(os.environ.get("PUB_HOLD_S", "42"))
CADDY = os.environ.get("CADDY", "http://localhost")
PUB_PORT = int(os.environ.get("PUB_PORT", "8899"))
EVID = HERE / "contract-loop-raw.json"


def _post(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{CADDY}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:  # 게이트/에러 응답도 본문 회수
        return e.code, {"_error": e.read().decode()[:300]}


def _get_signals() -> dict[str, Any]:
    url = f"{CADDY}/analysis/signals?sessionId={SESSION_ID}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def _summ(sig: dict[str, Any]) -> str:
    recs = sig.get("records", [])
    kinds: dict[str, int] = {}
    for rec in recs:
        kinds[rec.get("kind", "?")] = kinds.get(rec.get("kind", "?"), 0) + 1
    tf = (sig.get("transcriptFull") or "").strip()
    return f"records={len(recs)} kinds={kinds} transcriptFull[{len(tf)}자]={tf[:60]!r}"


def _start_httpd() -> "subprocess.Popen[bytes]":
    # pub.html + lk-sdk.mjs(2.7.5)를 상대 import 가능하게 slice10 디렉터리를 정적 서빙.
    return subprocess.Popen(
        ["python3", "-m", "http.server", str(PUB_PORT), "--bind", "127.0.0.1"],
        cwd=str(SLICE10), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _launch_pub() -> "subprocess.Popen[bytes]":
    env = dict(os.environ)
    env["GILJOBE_SMOKE_ROOM_ID"] = SESSION_ID
    env["PUB_HOLD_S"] = str(PUB_HOLD_S)
    env["PUB_PORT"] = str(PUB_PORT)
    return subprocess.Popen(
        [".venv/bin/python", "-u", str(SLICE10 / "drive_browser_pub.py")],
        cwd="/home/kio/workspace/giljobe", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def main() -> int:
    snaps: list[dict[str, Any]] = []
    print(f"[s11] SESSION_ID={SESSION_ID} room=giljob-session-{SESSION_ID} hold={PUB_HOLD_S:g}s")

    httpd = _start_httpd()
    time.sleep(0.5)
    try:
        # 1) 계약으로 subscriber attach.
        code, body = _post("/analysis/subscriber/start",
                           {"sessionId": SESSION_ID, "criticMode": "window"})
        print(f"[s11] start → http {code} {body}")

        # 2) publisher 구동(같은 룸).
        pub = _launch_pub()
        print(f"[s11] publisher 기동 pid={pub.pid}")

        # 3) 발행 동안 signals 폴링.
        t0 = time.monotonic()
        posted_stop = False
        stop_resp: dict[str, Any] = {}
        while time.monotonic() - t0 < PUB_HOLD_S + 8:
            time.sleep(5.0)
            try:
                sig = _get_signals()
            except Exception as e:  # noqa: BLE001
                print(f"[s11] signals 폴 실패: {e}")
                continue
            el = time.monotonic() - t0
            print(f"[s11] +{el:4.1f}s  {_summ(sig)}")
            snaps.append({"elapsed": round(el, 1), "summary": _summ(sig),
                          "recordCount": sig.get("recordCount"),
                          "transcriptFull": sig.get("transcriptFull")})
            # 4) media 살아있을 때(~hold 막판) flush → turn_end.eval 채워짐(Slice 10 권고).
            if not posted_stop and el >= PUB_HOLD_S - 8:
                code, stop_resp = _post("/analysis/subscriber/stop", {})
                posted_stop = True
                print(f"[s11] stop(flush) → http {code} {stop_resp}")

        if not posted_stop:
            code, stop_resp = _post("/analysis/subscriber/stop", {})
            print(f"[s11] stop(late) → http {code} {stop_resp}")

        time.sleep(2.0)
        final = _get_signals()
        print(f"[s11] FINAL  {_summ(final)}")
        te = final.get("latestTurnEnd")
        print(f"[s11] latestTurnEnd={'있음' if te else 'None'}")

        pub_out = b""
        try:
            pub_out = pub.communicate(timeout=10)[0] or b""
        except Exception:
            pub.kill()
        EVID.write_text(json.dumps({
            "sessionId": SESSION_ID, "snapshots": snaps, "stopResp": stop_resp,
            "final": final, "pubTail": pub_out.decode(errors="replace")[-1500:],
        }, ensure_ascii=False, indent=2))
        print(f"[s11] evidence → {EVID}")
        ok = (final.get("recordCount") or 0) > 0
        return 0 if ok else 4
    finally:
        httpd.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
