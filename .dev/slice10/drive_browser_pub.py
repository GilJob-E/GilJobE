#!/usr/bin/env python3
"""Slice 10 길 A — 호스트에서 **가짜장치 헤드리스 Chrome**으로 진짜 브라우저 publish를 구동.

그쪽 프론트 UI는 ai-engine(Gemini 크레딧 소진, 502)으로 마이크 게이팅이 막혀 못 쓴다. 그래서 그쪽
livekit-client SDK + 세션/토큰(POST /api/sessions candidate token)은 그대로 쓰되, 막힌 UI 버튼만
우회하는 최소 페이지(pub.html)를 자동 publish로 띄운다. 경로 자체는 진짜:
  getUserMedia(가짜장치=kor.y4m/kor.wav) → livekit-client SDK 인코딩 → LiveKit publish.

흐름:
  1) POST /api/sessions {sessionId} → candidateToken + url(ws://127.0.0.1:7880) + roomName.
  2) google-chrome --headless=new + 가짜장치 플래그 + --remote-debugging-port 로 pub.html?url=&token= 로드.
  3) Playwright connect_over_cdp 로 붙어 window.__pubState=='publishing' 까지 대기(콘솔 로그 캡처).
  4) PUB_HOLD_S 초 유지(그 동안 우리 subscriber가 캡처) 후 종료.

env: GILJOBE_SMOKE_ROOM_ID(기본 kio-smoke-10) · PUB_HOLD_S(기본 35) · CDP_PORT(기본 9222) · PUB_PORT(8899)
실행: PYTHONPATH=src .venv/bin/python .dev/slice10/drive_browser_pub.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOM_ID = os.environ.get("GILJOBE_SMOKE_ROOM_ID", "kio-smoke-10")
PUB_HOLD_S = float(os.environ.get("PUB_HOLD_S", "35"))
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
PUB_PORT = int(os.environ.get("PUB_PORT", "8899"))
CADDY = os.environ.get("GILJOBE_CADDY", "http://localhost")
Y4M = HERE / "kor.y4m"
WAV = HERE / "kor.wav"
USER_DATA = "/tmp/chrome-giljobe-smoke"


def create_session() -> dict[str, Any]:
    body = json.dumps({"sessionId": ROOM_ID, "interviewId": ROOM_ID, "role": "candidate"}).encode()
    req = urllib.request.Request(
        f"{CADDY}/api/sessions", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def launch_chrome(pub_url: str) -> "subprocess.Popen[bytes]":
    flags = [
        "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        f"--use-file-for-fake-video-capture={Y4M}",
        f"--use-file-for-fake-audio-capture={WAV}",
        "--autoplay-policy=no-user-gesture-required",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA}",
        pub_url,
    ]
    # chrome 자체 로그는 버리고(시끄러움) 프로세스만 띄운다.
    return subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    from playwright.sync_api import sync_playwright

    sess = create_session()
    lk = sess["livekit"]
    url = lk["url"]
    token = lk["candidateToken"]
    room = sess["roomName"]
    print(f"[drive] session room={room} url={url} (token hidden)")

    pub_url = (f"http://localhost:{PUB_PORT}/pub.html"
               f"?url={urllib.parse.quote(url)}&token={urllib.parse.quote(token)}")
    chrome = launch_chrome(pub_url)
    print(f"[drive] chrome 헤드리스 기동(pid={chrome.pid}), CDP :{CDP_PORT}")

    try:
        with sync_playwright() as pw:
            # chrome가 디버그 포트 열 때까지 잠깐 재시도 접속.
            browser = None
            for _ in range(40):
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
                    break
                except Exception:
                    time.sleep(0.25)
            if browser is None:
                print("[drive] ERROR: CDP 접속 실패")
                return 2
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.wait_for_event("page")
            logs: list[str] = []
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text[:200]}"))

            # publish 시작까지 대기(최대 25s).
            state = "?"
            for _ in range(100):
                try:
                    state = page.evaluate("window.__pubState")
                except Exception:
                    state = "?"
                if state and (state == "publishing" or str(state).startswith("error")):
                    break
                time.sleep(0.25)
            print(f"[drive] pub state = {state}")
            for ln in logs[-12:]:
                print(f"   [page] {ln}")
            if state != "publishing":
                return 3

            # 유지(우리 subscriber가 캡처). 진행 중 콘솔 일부 노출.
            print(f"[drive] {PUB_HOLD_S:g}s 유지 — 우리 subscriber가 캡처 중…")
            time.sleep(PUB_HOLD_S)
            try:
                final = page.evaluate("window.__pubState")
            except Exception:
                final = "?"
            # ★ 미디어가 살아있는 동안 즉시 flush 신호(sentinel) — subscriber가 실미디어 tail로 end_turn
            #   (빈 트레일링 윈도우·turn_end.eval=None 아티팩트 제거; Slice 10 검증 권고). chrome은 그 뒤 종료.
            stop = os.environ.get("GILJOBE_SMOKE_STOP", "/tmp/giljobe_smoke_stop")
            Path(stop).touch()
            print(f"[drive] hold 종료 → sentinel touch({stop}). final state={final}  room={room}")
            return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    raise SystemExit(main())
