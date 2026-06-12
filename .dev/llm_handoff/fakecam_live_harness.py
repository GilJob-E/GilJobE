#!/usr/bin/env python3
"""가짜 카메라(브라우저 fake device) 라이브 하니스 — lk 파일 퍼블리시의 GOP 버스트 병리를 제거한
실 WebRTC 캡처 페이싱으로 같은 문장 레인 턴을 재실행해, 영상 의존 수치(visual/nv 커버리지)를
절대값으로 재측정한다. 비교 기준선 = qa/archive/{clip}-signals-sentence-mode.json (lk-file 런).

구성: Chrome --headless=new + --use-file-for-fake-{video,audio}-capture(y4m/wav)가 실제
livekit-client 페이지로 룸에 퍼블리시(실 WebRTC 캡처 페이싱) ‖ 이 하니스가 API 포워더 역할로
sentence_live_harness.CLIPS의 sideband 이벤트를 그대로 주입. 페이지는 publish 완료 시
beacon POST(/published)를 쏘고 그 시각이 t0가 된다(룸 연결 후 디바이스를 열어 미디어 t≈0 정렬).

사용: python3 fakecam_live_harness.py kor
  LIVEKIT_API_KEY/SECRET는 셸 env 또는 giljob-docker/.env에서 읽는다(출력 안 함).
출력: giljob-docker/qa/runs/<YYYY-MM-DD>-fakecam-coverage/{signals,latency,meta}.json

미디어 변환 레시피(/tmp/qa-media 소실 시 — Chrome fake device는 y4m/wav만 받음):
  ffmpeg -i qa/media/kor.mp4 -vf scale=540:960 -pix_fmt yuv420p -r 30 /tmp/qa-media/kor-540.y4m
  ffmpeg -i qa/media/kor.mp4 -vn -ar 48000 -ac 1 /tmp/qa-media/kor-48k.wav
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sentence_live_harness import CLIPS, _post, _signals

DOCKER_REPO = Path("/home/kio/workspace/giljob-docker")
QA = DOCKER_REPO / "qa"
LIVEKIT_WS = "ws://127.0.0.1:17880"
HTTP_PORT = 8099
ENGINE_CONTAINER = "giljob-qa-analysis-engine-1"

MEDIA = {
    "kor": {"y4m": "/tmp/qa-media/kor-540.y4m", "wav": "/tmp/qa-media/kor-48k.wav",
            "width": 540, "height": 960},
}

# 실측 비교성 우선: 오디오 처리 OFF(lk 기준선은 raw ogg). 실서비스 브라우저 기본값(AEC 등 ON)은
# 별도 변수 — 이 런은 '딜리버리 페이싱'만 바꾼다. meta.json에 기록.
PAGE = """<!doctype html><meta charset=utf-8><title>fakecam</title>
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
<script>
(async () => {
  const cfg = await (await fetch('/token')).json();
  const room = new LivekitClient.Room();
  await room.connect(cfg.url, cfg.token);
  // 룸 연결 후에 디바이스를 열어 fake 파일 재생 시작(t=0)을 publish 직전으로 정렬
  const tracks = await LivekitClient.createLocalTracks({
    audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false},
    video: {resolution: {width: cfg.width, height: cfg.height, frameRate: 30}},
  });
  for (const t of tracks) await room.localParticipant.publishTrack(t);
  await fetch('/published', {method: 'POST'});
})().catch(e => fetch('/errlog', {method: 'POST', body: String((e && e.stack) || e)}));
</script>"""


def _poller2(session_id: str, seen_sent: dict, seen_eval: dict, stop_flag: threading.Event) -> None:
    """sentence + 발화중 eval 레코드의 first-seen wall 시각(0.4s 폴링) — eval 꼬리 측정용."""
    while not stop_flag.is_set():
        try:
            for r in _signals(session_id).get("records", []):
                if r.get("type") == "sentence":
                    key = (r["start_s"], r["end_s"])
                    seen_sent.setdefault(key, {"wall": time.time(), "record": r})
                elif r.get("type") == "eval":
                    ev = r.get("eval") or {}
                    key = (ev.get("window_start_s"), ev.get("window_dur_s"))
                    seen_eval.setdefault(key, {"wall": time.time(), "record": r})
        except Exception:  # noqa: BLE001 - 폴링 실패는 다음 주기에 재시도
            pass
        time.sleep(0.4)


def _livekit_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    env_file = (DOCKER_REPO / ".env").read_text().splitlines()
    for k in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        v = os.environ.get(k) or next(
            (ln.split("=", 1)[1].strip() for ln in env_file if ln.startswith(k + "=")), None)
        if not v:
            raise SystemExit(f"{k} 미설정 (셸 env / giljob-docker/.env)")
        keys[k] = v
    return keys


def _mint_token(keys: dict[str, str], room: str, identity: str = "qa-candidate") -> str:
    out = subprocess.run(
        ["docker", "run", "--rm",
         "-e", f"LIVEKIT_API_KEY={keys['LIVEKIT_API_KEY']}",
         "-e", f"LIVEKIT_API_SECRET={keys['LIVEKIT_API_SECRET']}",
         "livekit/livekit-cli", "token", "create", "--join",
         "--room", room, "--identity", identity, "--valid-for", "2h"],
        capture_output=True, text=True, check=True, timeout=60).stdout
    m = re.search(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", out)
    if not m:
        raise SystemExit("토큰 파싱 실패 (lk token create 출력 형식 변경?)")
    return m.group(0)


class _Hub:
    """페이지 ↔ 하니스 연결점: 토큰 서빙 + publish beacon + 페이지 JS 에러 수집."""

    def __init__(self, token: str, media: dict):
        self.token_payload = json.dumps({
            "url": LIVEKIT_WS, "token": token,
            "width": media["width"], "height": media["height"],
        }).encode()
        self.published = threading.Event()
        self.page_error: str | None = None


def _make_handler(hub: _Hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 기본 stderr 액세스 로그 침묵
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/token":
                self._send(200, hub.token_payload, "application/json")
            else:
                self._send(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/published":
                hub.published.set()
                self._send(204)
            elif self.path == "/errlog":
                hub.page_error = body.decode(errors="replace")
                self._send(204)
            else:
                self._send(404)

    return Handler


def _chrome(media: dict, profile: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-first-run", "--disable-gpu",
         f"--user-data-dir={profile}",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
         f"--use-file-for-fake-video-capture={media['y4m']}",
         f"--use-file-for-fake-audio-capture={media['wav']}",
         "--autoplay-policy=no-user-gesture-required",
         f"http://127.0.0.1:{HTTP_PORT}/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _engine_meta() -> dict:
    out = subprocess.run(
        ["docker", "inspect", ENGINE_CONTAINER,
         "--format", '{{.Config.Image}}|{{range .Config.Env}}{{println .}}{{end}}'],
        capture_output=True, text=True, check=True).stdout
    image, _, env_blob = out.partition("|")
    env = dict(ln.split("=", 1) for ln in env_blob.splitlines() if "=" in ln)
    return {
        "image": image.strip(),
        "env": {k: env.get(k) for k in
                ("GILJOBE_GIT_REF", "GILJOBE_TRANSCRIPT_SOURCE", "GILJOBE_VISION", "GILJOBE_PROSODY")},
    }


def run(clip_name: str, exp: str = "fakecam-coverage") -> None:
    clip, media = CLIPS[clip_name], MEDIA[clip_name]
    for f in (media["y4m"], media["wav"]):
        if not Path(f).exists():
            raise SystemExit(f"미디어 없음: {f} (ffmpeg 변환 먼저 — qa/README.md)")
    session_id = f"qa-{exp}-{clip_name}"
    run_dir = QA / "runs" / f"{time.strftime('%Y-%m-%d')}-{exp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    keys = _livekit_keys()
    token = _mint_token(keys, f"giljob-session-{session_id}")
    hub = _Hub(token, media)
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), _make_handler(hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(_post("/subscriber/start", {"sessionId": session_id, "criticMode": "window"}))
    profile = tempfile.mkdtemp(prefix="fakecam-chrome-")
    chrome = _chrome(media, profile)
    try:
        if not hub.published.wait(timeout=40):
            raise SystemExit(f"publish beacon 타임아웃 — 페이지 에러: {hub.page_error!r}")
        t0 = time.time()
        print(f"published (Chrome→LiveKit) — t0 고정, 이벤트 리플레이 시작")

        seen: dict = {}
        seen_eval: dict = {}
        stop_flag = threading.Event()
        threading.Thread(target=_poller2, args=(session_id, seen, seen_eval, stop_flag),
                         daemon=True).start()

        sent_log: list[dict] = []
        for (t_rel, kind, item, text) in clip["events"]:
            time.sleep(max(0.0, t0 + t_rel - time.time()))
            payload: dict = {
                "schema_version": "2026-06-11.realtime-mmm-ingress.v1",
                "source": "qa-harness-fakecam", "sessionId": session_id, "eventKind": kind,
            }
            if item or text:
                payload["detail"] = {"itemId": item, "transcript": text}
            resp = _post("/realtime/turn-events", payload)
            if kind == "transcript.completed":
                sent_log.append({"item": item, "t_rel": t_rel, "wall": time.time(),
                                 "accepted": resp.get("accepted"),
                                 "sentences": resp.get("sentences")})
        time.sleep(max(0.0, t0 + clip["publish_s"] - time.time()))
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
        httpd.shutdown()
    time.sleep(2.0)
    stop_t0 = time.time()
    _post("/subscriber/stop", {})
    stop_dur = time.time() - stop_t0

    final = _signals(session_id)
    (run_dir / "signals.json").write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n")

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
    # 발화중 eval 꼬리: 윈도우의 미디어상 끝(t0+start+dur) → 레코드 first-seen.
    # A(16s 벽시계)/B(문장 정렬+slim) 비교의 1차 지표 — 폴링 0.4s 분해능.
    eval_rows = []
    for ((ws, wd), info) in sorted(seen_eval.items(), key=lambda kv: kv[0][0] or 0):
        ev = info["record"].get("eval") or {}
        eval_rows.append({
            "window": [ws, wd],
            "tail_s": round(info["wall"] - (t0 + (ws or 0) + (wd or 0)), 2),
            "vocal_axis_present": bool(ev.get("vocal")),
            "visual_axis_present": bool(ev.get("visual")),
        })
    report = {
        "clip": clip_name, "sessionId": session_id,
        "sentences_observed_live": len(seen),
        "completed_events": sent_log,
        "per_sentence": rows,
        "eval_windows": eval_rows,
        "stop_to_turn_end_s": round(stop_dur, 2),
        "note": "latency = completed 이벤트 송신 → /signals에서 레코드 first-seen(폴링 0.4s 분해능)",
    }
    (run_dir / "latency.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    chrome_ver = subprocess.run(["google-chrome", "--version"],
                                capture_output=True, text=True).stdout.strip()
    meta = {
        "date": time.strftime("%Y-%m-%d"),
        "purpose": "lk 파일 퍼블리시 GOP 버스트 제거 시 visual/nv 커버리지 절대값 재측정 "
                   "(기준선: qa/archive/kor-signals-sentence-mode.json)",
        "engine": _engine_meta(),
        "engine_note": "GILJOBE_GIT_REF는 빌드 시점 env — 실효 코드는 문장 레인 핫패치 포함 "
                       "(2026-06-12 핸드오프: 6bef78b 핀 + dae5191 모듈 docker cp)",
        "media": f"qa/media/{clip_name}.mp4 → {media['y4m']} (540x960@30) + {media['wav']} (48k mono)",
        "publish_path": "fakecam-browser",
        "publisher": f"{chrome_ver} --headless=new --use-file-for-fake-{{video,audio}}-capture, "
                     "livekit-client@2 (CDN), 오디오 처리 OFF(AEC/NS/AGC — lk 기준선과 비교성 우선)",
        "transcript_feed": "harness-sideband (sentence_live_harness.CLIPS 재사용)",
        "stack": "giljob-qa",
        "caveat": "y4m은 캡처 시작부터 루프 — publish_s(42s) > 클립(38.2s)이라 마지막 ~4s는 "
                  "선두 구간 재생(꼬리 윈도우 오염 가능, lk 런의 유휴 꼬리와 유사한 한계)",
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    print(json.dumps({k: report[k] for k in ("sentences_observed_live", "stop_to_turn_end_s")}))
    for r in rows:
        print(f"  {r['sentence']} src={r['source']} lat={r['latency_s']}s "
              f"ch={r['channels']} chars={r['text_chars']}")
    print(f"→ {run_dir}")


if __name__ == "__main__":
    run(sys.argv[1], *(sys.argv[2:3] or []))
