"""실측 레이턴시 (.dev 스크래치) — kor.mp4를 38.27초 실시간 스트림처럼 흘리며
PLAN §5의 3레인 병렬(nv_exec / stt_exec / eval_exec)을 흉내 내, **윈도우가 닫히고
신호가 나오기까지의 실질 레이턴시**를 잰다.

핵심 측정:
  - latency_after_close: 윈도우 종료 시점 → 신호 ready (제품 관점 실질 지연)
  - queue_wait: 레인이 밀렸는지(이전 윈도우가 안 끝나 대기했는지)
  - service: 워커 처리시간(assemble mp4 인코드 + 모델 — 라이브 워커의 진짜 비용)
  - real_time_tail: 스트림이 끝나고 마지막 신호가 나올 때까지

프레임/PCM은 타이밍 전에 미리 추출한다(라이브에선 WebRTC가 이미 이미지로 줌 —
소스 디코드는 파이프라인 비용이 아님). assemble(ffmpeg mp4 인코드)는 워커 안에서 측정.

재현: vLLM 기동 후 `PYTHONPATH=src .venv/bin/python .dev/measure_latency.py`
"""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
import tempfile
import time
from pathlib import Path

from giljobe.analysis.critic import WindowCritic
from giljobe.llm.vllm_client import default_vllm_client

CLIP = Path(__file__).parent.parent / "kor.mp4"
SR = 16000
STREAM_DUR = 38.27  # ffprobe 실측
NV = 3.0
OUT = Path(__file__).parent / "latency_sim.json"


def pcm(ss: float, dur: float) -> bytes:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(CLIP),
           "-vn", "-ar", str(SR), "-ac", "1", "-f", "s16le", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=30).stdout


def frames(ss: float, dur: float, fps: float = 1.0) -> list[bytes]:
    with tempfile.TemporaryDirectory() as d:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{ss:g}", "-t", f"{dur:g}", "-i", str(CLIP),
               "-vf", f"fps={fps:g}", "-q:v", "3", str(Path(d, "f%05d.jpg"))]
        subprocess.run(cmd, capture_output=True, timeout=30)
        return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"))]


# --- 타이밍 전에 모든 윈도우 미디어 미리 추출 ---
nv_windows = []
i = 0
while i * NV < STREAM_DUR:
    ss = i * NV
    close = min(ss + NV, STREAM_DUR)
    nv_windows.append({"i": i, "ss": ss, "close": close,
                       "frames": frames(ss, close - ss), "pcm": pcm(ss, close - ss)})
    i += 1
eval_windows = [{"ss": 0, "close": 16.0}, {"ss": 16, "close": 32.0}]
for e in eval_windows:
    e["frames"] = frames(e["ss"], e["close"] - e["ss"])
    e["pcm"] = pcm(e["ss"], e["close"] - e["ss"])
tail = {"ss": 32, "close": STREAM_DUR, "frames": frames(32, STREAM_DUR - 32), "pcm": pcm(32, STREAM_DUR - 32)}

client = default_vllm_client()
client.health()
critic = WindowCritic(client=client)
critic.warmup()  # 텍스트 그래프 선warm (멀티모달 cold는 첫 윈도우에 그대로 남겨 정직하게 측정)

nv_exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
stt_exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
eval_exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
futures: list = []
t0 = time.perf_counter()


def submit(exe, lane: str, idx, close: float, fn):
    enq = time.perf_counter()

    def work():
        start = time.perf_counter()
        ok = True
        try:
            fn()
        except Exception:  # noqa: BLE001 - 라이브 레인 워커처럼 실패를 흡수(eot tail이 재커버)
            ok = False
        end = time.perf_counter()
        return {"lane": lane, "i": idx, "close": close, "enqueue": enq, "start": start, "end": end, "ok": ok}
    futures.append(exe.submit(work))


def wait_until(stream_t: float):
    d = (t0 + stream_t) - time.perf_counter()
    if d > 0:
        time.sleep(d)


# 스트림 시간순 이벤트(윈도우 종료 시점에 해당 레인에 submit)
events = [(w["close"], "nvstt", w) for w in nv_windows]
events += [(e["close"], "eval", e) for e in eval_windows]
events.append((STREAM_DUR, "tail", tail))
events.sort(key=lambda x: x[0])

for close, kind, w in events:
    wait_until(close)
    if kind == "nvstt":
        dur = w["close"] - w["ss"]
        submit(nv_exec, "nv", w["i"], close,
               lambda w=w, dur=dur: critic.read_nonverbal(w["frames"], w["pcm"], t=w["ss"] + dur / 2, window_s=dur))
        submit(stt_exec, "stt", w["i"], close,
               lambda w=w, dur=dur: critic.transcribe_window(w["pcm"], t=w["ss"], window_s=dur))
    elif kind == "eval":
        submit(eval_exec, "eval", f'{w["ss"]:g}-{w["close"]:g}', close,
               lambda w=w: critic.evaluate_window(w["frames"], w["pcm"], window_start_s=w["ss"], window_dur_s=w["close"] - w["ss"]))
    else:  # tail = compact eval (end-of-turn 빠른 경로)
        submit(eval_exec, "tail", "compact", close,
               lambda w=w: critic.evaluate_window(w["frames"], w["pcm"], window_start_s=w["ss"], window_dur_s=w["close"] - w["ss"], compact=True))

stream_done = time.perf_counter()
results = [f.result() for f in futures]  # 모든 레인 드레인 대기
all_done = time.perf_counter()
for ex in (nv_exec, stt_exec, eval_exec):
    ex.shutdown()

for r in results:
    r["latency_after_close"] = r["end"] - (t0 + r["close"])
    r["queue_wait"] = r["start"] - r["enqueue"]
    r["service"] = r["end"] - r["start"]


def stat(xs):
    xs = sorted(xs)
    n = len(xs)
    p95 = xs[min(n - 1, int(round(0.95 * (n - 1))))]
    return {"n": n, "mean": round(sum(xs) / n, 3), "max": round(xs[-1], 3), "p95": round(p95, 3)}


report = {"clip": CLIP.name, "stream_dur_s": STREAM_DUR, "lanes": {}}
print("=" * 74)
print(f"스트림 길이: {STREAM_DUR}s | 3초 윈도우 {len(nv_windows)}개 | 16초 eval 2개 + compact tail 1개")
print("=" * 74)
for lane in ("nv", "stt", "eval", "tail"):
    rs = [r for r in results if r["lane"] == lane]
    if not rs:
        continue
    lat = stat([r["latency_after_close"] for r in rs])
    svc = stat([r["service"] for r in rs])
    qw = stat([r["queue_wait"] for r in rs])
    fails = sum(1 for r in rs if not r.get("ok", True))
    report["lanes"][lane] = {"latency_after_close": lat, "service": svc, "queue_wait": qw, "fails": fails}
    print(f"\n[{lane}] n={lat['n']}" + (f"  ⚠️ JSON 절단 실패 {fails}개" if fails else ""))
    print(f"  service(처리=인코드+모델)  mean {svc['mean']}s  max {svc['max']}s  p95 {svc['p95']}s")
    print(f"  latency_after_close(실질) mean {lat['mean']}s  max {lat['max']}s  p95 {lat['p95']}s")
    print(f"  queue_wait(레인 밀림)      mean {qw['mean']}s  max {qw['max']}s  ({'백로그 있음!' if qw['max'] > 0.3 else '밀림 없음'})")

# nv 레인이 3초 cadence를 못 따라간 윈도우(service > 3s)?
nv_overrun = [r["i"] for r in results if r["lane"] == "nv" and r["service"] > NV]
real_time_tail = all_done - (t0 + STREAM_DUR)
total_wall = all_done - t0
report.update({
    "real_time_tail_s": round(real_time_tail, 3),
    "total_wall_s": round(total_wall, 3),
    "nv_overrun_windows": nv_overrun,
    "stream_emit_wall_s": round(stream_done - t0, 3),
})
print("\n" + "-" * 74)
print(f"nv 레인 cadence 초과(service>3s) 윈도우: {nv_overrun or '없음'}")
print(f"스트림 종료({STREAM_DUR}s) 후 마지막 신호까지(real-time tail): {real_time_tail:.2f}s")
print(f"전체 벽시계(t0→마지막 신호): {total_wall:.2f}s   (스트림 {STREAM_DUR}s 대비 {total_wall - STREAM_DUR:+.2f}s)")
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n저장: {OUT}")
