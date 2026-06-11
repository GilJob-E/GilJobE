#!/usr/bin/env python3
"""문장 레인(외부 전사 모드) 라이브 하니스 — QA 스택에 실 미디어 + 합성 sideband 이벤트를 흘려
sentence 레코드의 채널 충실도와 **실질 레이턴시**(이벤트 송신→레코드 관측)를 측정한다.

브라우저/OpenAI Realtime 없이 PR #13 계약의 우리 쪽 절반을 검증한다:
  lk 파일 퍼블리시(실 미디어) ‖ 이 하니스가 API 포워더 역할(완료/VAD 이벤트를
  /analysis/realtime/turn-events로 POST, detail.transcript 포함) → /signals 폴링으로
  sentence 레코드 first-seen 시각 기록 → 레이턴시 = first_seen − completed 송신.

사용: python3 sentence_live_harness.py kor|vid33  (LIVEKIT_API_KEY/SECRET는 셸 env)
출력: giljob-docker/qa/{clip}-signals-sentence-mode.json + {clip}-sentence-latency.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8081/analysis"
QA = Path("/home/kio/workspace/giljob-docker/qa")

CLIPS = {
    "kor": {
        "h264": "kor.h264", "ogg": "kor.ogg", "publish_s": 42,
        "events": [
            (1.0, "vad.speech_started", None, None),
            (14.5, "vad.speech_stopped", None, None),
            (15.2, "transcript.completed", "i1",
             "안녕하십니까? 지원자 이나래입니다. 저는 그때 동기들에게 잠은 대체 언제 자냐는 "
             "얘기를 자주 들었습니다. 그 이유는 왕복 네 시간 통학하면서도 주말 아르바이트에 "
             "한 번도 지각하지 않았고 성적 장학금도 계속 받았기 때문입니다."),
            (15.4, "vad.speech_started", None, None),
            (29.5, "vad.speech_stopped", None, None),
            (30.2, "transcript.completed", "i2",
             "이런 경험을 기반으로 공정개선 공모전에 나갔을 때는 생산 속도를 6% 단축시키는 "
             "결과를 냈고 대상도 받을 수 있었습니다. 현재 회사에서도 신규 장비를 셋업할 때 "
             "50번 이상 파라미터 테스트를 진행해서 공정 최적화에 도움을 줄 수 있었습니다."),
            (30.4, "vad.speech_started", None, None),
            (38.0, "vad.speech_stopped", None, None),
            (38.6, "transcript.completed", "i3",
             "앞으로 SK하이닉스에서도 차세대 메모리를 효율적으로 양산하는 데 기여하고 "
             "싶습니다. 감사합니다."),
        ],
    },
    "vid33": {
        "h264": "vid33.h264", "ogg": "vid33.ogg", "publish_s": 54,
        "events": [
            (1.0, "vad.speech_started", None, None),
            (19.0, "vad.speech_stopped", None, None),
            (19.8, "transcript.completed", "i1",
             "Good morning, my name is Anish. I am a third year graduate from Manipal "
             "University. I am pursuing computer science engineering with artificial "
             "intelligence and machine learning."),
            (20.2, "vad.speech_started", None, None),
            (32.5, "vad.speech_stopped", None, None),
            (33.2, "transcript.completed", "i2",
             "This combination of AI and design has fueled my passion for full stack "
             "development and machine learning. I worked as a machine learning intern "
             "at TechNook for a month."),
            (33.5, "vad.speech_started", None, None),
            (49.0, "vad.speech_stopped", None, None),
            (49.6, "transcript.completed", "i3",
             "I have been developing web applications for more than six months now. "
             "Things like that."),
        ],
    },
}


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _signals(session_id: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/signals?sessionId={session_id}", timeout=10) as r:
        return json.loads(r.read())


def _publisher(clip: dict, session_id: str) -> subprocess.Popen:
    """lk 파일 퍼블리시(버스트 딜리버리 — NV 버그를 노출시킨 그 조건 그대로)."""
    return subprocess.Popen(
        ["docker", "run", "--rm", "--network", "host", "-v", "/tmp/qa-media:/work:ro",
         "-e", "LIVEKIT_URL=ws://127.0.0.1:17880",
         "-e", f"LIVEKIT_API_KEY={os.environ['LIVEKIT_API_KEY']}",
         "-e", f"LIVEKIT_API_SECRET={os.environ['LIVEKIT_API_SECRET']}",
         "livekit/livekit-cli", "room", "join", "--identity", "qa-candidate",
         "--publish", f"/work/{clip['h264']}", "--publish", f"/work/{clip['ogg']}",
         "--fps", "30", f"giljob-session-{session_id}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _wait_join(pub: subprocess.Popen, fallback_s: float = 8.0) -> None:
    """lk가 룸에 붙은 로그를 본 시점을 t0로 — docker 기동 지연(~3-5s)이 이벤트 스케줄을
    미디어 시계와 어긋나게 하지 않도록(실 PR13은 브라우저 실시간 전송이라 이 왜곡이 없음)."""
    deadline = time.time() + fallback_s
    assert pub.stdout is not None
    for line in pub.stdout:
        if "connected" in line.lower() or "publish" in line.lower() or time.time() > deadline:
            break
    threading.Thread(target=lambda: [None for _ in pub.stdout], daemon=True).start()  # 파이프 드레인


def _poller(session_id: str, seen: dict, stop_flag: threading.Event) -> None:
    """/signals를 0.4s 간격 폴링 — sentence 레코드 (start,end)별 first-seen wall 시각."""
    while not stop_flag.is_set():
        try:
            for r in _signals(session_id).get("records", []):
                if r.get("type") == "sentence":
                    key = (r["start_s"], r["end_s"])
                    seen.setdefault(key, {"wall": time.time(), "record": r})
        except Exception:  # noqa: BLE001 - 폴링 실패는 다음 주기에 재시도
            pass
        time.sleep(0.4)


def run(clip_name: str) -> None:
    clip = CLIPS[clip_name]
    session_id = f"qa-sentence-{clip_name}"
    print(_post("/subscriber/start", {"sessionId": session_id, "criticMode": "window"}))
    pub = _publisher(clip, session_id)
    _wait_join(pub)
    t0 = time.time()
    seen: dict = {}
    stop_flag = threading.Event()
    threading.Thread(target=_poller, args=(session_id, seen, stop_flag), daemon=True).start()

    sent_log: list[dict] = []  # completed 이벤트 송신 wall 시각(레이턴시 기준점)
    for (t_rel, kind, item, text) in clip["events"]:
        time.sleep(max(0.0, t0 + t_rel - time.time()))
        payload: dict = {
            "schema_version": "2026-06-11.realtime-mmm-ingress.v1",
            "source": "qa-harness", "sessionId": session_id, "eventKind": kind,
        }
        if item or text:
            payload["detail"] = {"itemId": item, "transcript": text}
        resp = _post("/realtime/turn-events", payload)
        if kind == "transcript.completed":
            sent_log.append({"item": item, "t_rel": t_rel, "wall": time.time(),
                             "accepted": resp.get("accepted"), "sentences": resp.get("sentences")})
    time.sleep(max(0.0, t0 + clip["publish_s"] - time.time()))
    pub.terminate()
    time.sleep(2.0)
    stop_t0 = time.time()
    _post("/subscriber/stop", {})
    stop_dur = time.time() - stop_t0

    final = _signals(session_id)
    (QA / f"{clip_name}-signals-sentence-mode.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2) + "\n")

    # 레이턴시: 각 sentence 레코드 first-seen − 그 구간을 만든 completed 송신 시각.
    # (폴링 0.4s 분해능 — 실제 지연은 관측치보다 최대 0.4s 짧을 수 있음)
    rows = []
    for (key, info) in sorted(seen.items()):
        producer = max((s for s in sent_log if s["wall"] <= info["wall"]),
                       key=lambda s: s["wall"], default=None)
        rows.append({
            "sentence": key, "text_chars": len(info["record"].get("text") or ""),
            "source": info["record"].get("source"),
            "latency_s": round(info["wall"] - producer["wall"], 2) if producer else None,
            "channels": {c: info["record"].get(c) is not None
                         for c in ("speech", "visual", "nonverbal")},
        })
    report = {
        "clip": clip_name, "sessionId": session_id,
        "sentences_observed_live": len(seen),
        "completed_events": sent_log,
        "per_sentence": rows,
        "stop_to_turn_end_s": round(stop_dur, 2),
        "note": "latency = completed 이벤트 송신 → /signals에서 레코드 first-seen(폴링 0.4s 분해능)",
    }
    (QA / f"{clip_name}-sentence-latency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("sentences_observed_live", "stop_to_turn_end_s")}))
    for r in rows:
        print(f"  {r['sentence']} src={r['source']} lat={r['latency_s']}s ch={r['channels']} chars={r['text_chars']}")


if __name__ == "__main__":
    run(sys.argv[1])
