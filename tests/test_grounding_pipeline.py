"""test_grounding_pipeline — 객관 그라운딩 레인의 "inject AND emit" 배선 통합 검증 (GPU 불요).

커버(레인 모듈 단위 검증은 test_prosody/test_grounding, 여기는 *배선*):
  - 윈도워: grounder/prosody 주입 → nv/eval/tail 워커가 수치를 계산해 critic에 넘기고(inject)
    signal(.objective/.objective_vocal/.objective_visual)에 raw로 붙인다(emit). 레인 실패는
    수치 누락으로 강등(추론 레인 무영향). reset/close가 grounder 수명을 함께 구동.
  - recorder/emit: NonVerbalSignal.objective → WindowRecord.objective_nonverbal,
    EvaluationSignal.objective_* → eval 레코드에 보존.
  - critic: 수치가 프롬프트 텍스트에 한국어 라벨+차단 규칙으로 주입된다(fake vLLM client로 캡처).
  - ingest/구독자: consume_video_lk dense 콜백이 throttle 전 풀 fps로 grounder에 흐른다.
"""
from __future__ import annotations

import asyncio
import io
import json

from PIL import Image

from giljobe.analysis.mocks import MockCritic
from giljobe.emit.sink import SignalRecorder
from giljobe.media.ingest import consume_video_lk
from giljobe.models.signals import EvaluationSignal, NonVerbalSignal, TranscriptSignal
from giljobe.pipeline.windowing import InlineExecutor, TurnWindower

VISION_M = {"face_frames": 90, "face_seen_ratio": 1.0, "smile_mean": 0.4, "smile_max": 0.8,
            "smile_ratio": 0.7, "blink_count": 2, "gaze_off_mean": 0.2, "yaw_std_deg": 1.1,
            "pitch_std_deg": 0.5, "lip_motion": 0.002, "brow_down_mean": 0.0,
            "mouth_press_mean": 0.0, "pose_frames": 90, "pose_seen_ratio": 1.0,
            "posture_std": 0.02, "head_sway": 0.01, "wrist_visible": 0.0,
            "wrist_motion": 0.0, "gesture_events": 0}
PROSODY_M = {"duration_s": 3.0,
             "pitch": {"voiced_frames": 100, "sd_semitone": 4.3, "pdq": 0.26,
                       "range_st_10_90": 9.0, "detrended_sd_semitone": 3.9,
                       "voiced_ratio": 0.8, "mean_hz": 220.0, "octave_error_frac": 0.0},
             "rate": {"syllable_nuclei": 18, "speech_rate_syl_per_s": 6.0,
                      "articulation_rate_syl_per_s": 6.4},
             "pauses": {"pause_count_ge_0p25": 0, "short_juncture_count_0p1_0p25": 5,
                        "juncture_median_s": 0.12, "longest_pauses_s": []},
             "energy": {"rel_db_slope_per_s": 0.1, "half_to_half_delta_db": 0.4,
                        "sounding_trough_db": -20.0}}


class FakeGrounder:
    """_Grounder 표면 더블 — 호출 기록 + 고정 집계 반환(입력의 순수 함수, mocks.py 관례)."""

    def __init__(self, metrics=VISION_M):
        self.metrics = metrics
        self.frames: list[tuple[float, object]] = []
        self.resets = 0
        self.closed = False

    def add_frame(self, t_s, frame):
        self.frames.append((t_s, frame))

    def window_metrics(self, start_s, end_s):
        return self.metrics

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


class RaisingGrounder(FakeGrounder):
    def window_metrics(self, start_s, end_s):
        raise RuntimeError("grounding boom")


class FakeProsody:
    def __init__(self, metrics=PROSODY_M):
        self.metrics = metrics
        self.calls = 0

    def extract(self, pcm, *, sample_rate=16000):
        self.calls += 1
        return self.metrics if pcm else None


def _fill(w: TurnWindower, dur: float) -> None:
    t = 0.0
    while t < dur:
        w.add_frame(t, b"jpeg")
        w.add_audio(t, b"pcm")
        t += 0.5


def _by_type(sigs, cls):
    return [s for s in sigs if isinstance(s, cls)]


# ─────────────────────────── 윈도워 inject AND emit ───────────────────────────

def test_windower_attaches_objectives_to_all_signals():
    """nv→objective, eval→objective_vocal/visual, turn_end compact tail에도 동일하게 붙는다."""
    sigs = []
    g, p = FakeGrounder(), FakeProsody()
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor(),
                     eval_window_s=3.0, grounder=g, prosody=p)
    _fill(w, 5.0)  # [3,5)까지 채워야 compact tail이 평가할 미디어가 있다
    w.poll(3.0)
    te = w.end_turn(5.0)

    nv = _by_type(sigs, NonVerbalSignal)
    ev = [s for s in _by_type(sigs, EvaluationSignal) if not s.compact]
    assert nv and all(s.objective == VISION_M for s in nv)
    assert ev and ev[0].objective_vocal == PROSODY_M and ev[0].objective_visual == VISION_M
    assert te.eval is not None and te.eval.compact
    assert te.eval.objective_vocal == PROSODY_M and te.eval.objective_visual == VISION_M
    assert p.calls >= 2  # 발화중 eval + compact tail


def test_windower_without_lanes_unchanged():
    """grounder/prosody 미주입 → objective 필드 None(기존 동작·스키마 그대로)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor(), eval_window_s=3.0)
    w.add_dense_frame(0.1, b"frame")  # grounder 없음 → no-op(예외 없음)
    _fill(w, 3.0)
    w.poll(3.0)
    assert all(s.objective is None for s in _by_type(sigs, NonVerbalSignal))
    assert all(s.objective_vocal is None and s.objective_visual is None
               for s in _by_type(sigs, EvaluationSignal))


def test_grounding_failure_degrades_to_none_not_lane_death():
    """grounder가 던져도 nv/eval은 정상 emit(수치만 None) — 레인 실패 격리."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor(),
                     eval_window_s=3.0, grounder=RaisingGrounder(), prosody=FakeProsody())
    _fill(w, 3.0)
    w.poll(3.0)
    nv = _by_type(sigs, NonVerbalSignal)
    ev = _by_type(sigs, EvaluationSignal)
    assert nv and nv[0].objective is None
    assert ev and ev[0].objective_visual is None and ev[0].objective_vocal == PROSODY_M


def test_windower_drives_grounder_lifecycle():
    g = FakeGrounder()
    w = TurnWindower(MockCritic(), executor=InlineExecutor(), grounder=g)
    w.add_dense_frame(0.5, b"raw-frame")
    assert g.frames == [(0.5, b"raw-frame")]
    w.reset()
    assert g.resets == 1
    w.close()
    assert g.closed


# ─────────────────────────── recorder/emit 보존 ───────────────────────────

class _ListSink:
    def __init__(self):
        self.records: list[dict] = []

    def emit(self, record):
        self.records.append(record)


def test_recorder_preserves_objective_fields():
    sink = _ListSink()
    rec = SignalRecorder("s1", [sink])
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="engaged", intensity=0.5,
                                  note="", objective=VISION_M))
    rec.on_signal(TranscriptSignal(t=1.5, window_s=3.0, text="안녕하세요"))
    rec.on_signal(EvaluationSignal(window_start_s=0.0, window_dur_s=3.0,
                                   objective_vocal=PROSODY_M, objective_visual=VISION_M))
    window = next(r for r in sink.records if r["type"] == "window")
    assert window["objective_nonverbal"] == VISION_M
    assert window["nonverbal"] == {"state": "engaged", "intensity": 0.5, "note": ""}
    ev = next(r for r in sink.records if r["type"] == "eval")
    assert ev["eval"]["objective_vocal"] == PROSODY_M
    assert ev["eval"]["objective_visual"] == VISION_M
    json.dumps(sink.records, ensure_ascii=False)  # 직렬화 가능(JSONL/SSE 계약)


# ─────────────────────────── critic 프롬프트 주입 (fake vLLM 캡처) ───────────────────────────

class _FakeClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.requests: list[dict] = []

    def chat(self, body: dict) -> dict:
        self.requests.append(body)
        return {"choices": [{"message": {"content": json.dumps(self._payload, ensure_ascii=False)}}]}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (90, 120, 150)).save(buf, format="JPEG")
    return buf.getvalue()


def _last_text(client: _FakeClient) -> str:
    content = client.requests[-1]["messages"][1]["content"]
    return next(p["text"] for p in content if p.get("type") == "text")


def test_critic_injects_grounding_block_into_prompts():
    """수치가 실제 프롬프트 텍스트에 라벨+차단 규칙으로 들어가는가(ffmpeg 경유 실 조립)."""
    from giljobe.analysis.critic import WindowCritic

    client = _FakeClient({"state": "neutral", "intensity": 0.2, "note": ""})
    critic = WindowCritic(client=client)
    pcm = b"\x00\x00" * 8000  # 0.5s 무음
    sig = critic.read_nonverbal([_jpeg()], pcm, t=1.5, window_s=3.0, vision=VISION_M)
    text = _last_text(client)
    assert "시각 측정치" in text and "관측 불가" in text and "단정" in text
    assert sig.state == "neutral"

    client2 = _FakeClient({"verbal": {}, "vocal": {}, "visual": {}, "critique": ["x"]})
    critic2 = WindowCritic(client=client2)
    critic2.evaluate_window([_jpeg()], pcm, window_start_s=0.0, window_dur_s=3.0,
                            prosody=PROSODY_M, vision=VISION_M)
    text2 = _last_text(client2)
    assert "음성 측정치" in text2 and "시각 측정치" in text2 and "억양 변화" in text2


def test_critic_without_metrics_prompt_unchanged():
    from giljobe.analysis.critic import WindowCritic

    client = _FakeClient({"state": "neutral", "intensity": 0.2, "note": ""})
    WindowCritic(client=client).read_nonverbal([_jpeg()], b"", t=1.5, window_s=3.0)
    assert _last_text(client) == "Read the candidate's non-verbal state now."


# ─────────────────────────── ingest dense 피드 (throttle 전, 풀 fps) ───────────────────────────

class _FakeStream:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self):
        self.closed = True


class _Ev:
    def __init__(self, frame, timestamp_us):
        self.frame, self.timestamp_us = frame, timestamp_us


class _Frame:
    def __init__(self, w=8, h=8):
        self.width, self.height = w, h
        self.data = bytes(w * h * 3)


class _WindowerDouble:
    def __init__(self):
        self.jpegs: list[float] = []

    def add_frame(self, t_s, jpeg):
        self.jpegs.append(t_s)

    def add_audio(self, t_s, pcm):
        pass


def test_consume_video_lk_dense_gets_full_fps_jpeg_still_throttled():
    """dense 콜백은 매 프레임(3회), JPEG 경로는 1fps throttle(1회) — 두 피드가 독립."""
    wd = _WindowerDouble()
    dense_calls: list[float] = []
    events = [_Ev(_Frame(), int(i * 100_000)) for i in range(3)]  # 0, 0.1s, 0.2s
    stream = _FakeStream(events)
    asyncio.run(consume_video_lk(stream, wd, dense=lambda t, f: dense_calls.append(t)))
    assert dense_calls == [0.0, 0.1, 0.2]
    assert wd.jpegs == [0.0]
    assert stream.closed
