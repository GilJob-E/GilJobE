"""test_emit_schema — WindowRecord/EvalRecord/TurnEndRecord 형태·nv+전사 병합·t-정렬·
JSONL/SSE 유효성 (GPU/브라우저 불요).

SignalRecorder를 (1) 직접 신호로 단위 검증하고, (2) 실 TurnWindower(InlineExecutor)+MockCritic
출력으로 배선 검증한다(emit이 윈도워 신호 스트림을 실제로 직렬화하는지). Sink는 JSONLSink(파일
라운드트립)·SSESink(프레임 포맷)·_ListSink(인메모리 단언)로 본다.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading

from giljobe.analysis.mocks import MockCritic, MockTranscriber, SlowMockCritic
from giljobe.emit.sink import JSONLSink, SignalRecorder, SSESink, sse_frame
from giljobe.models.signals import (
    EvaluationSignal,
    NonVerbalSignal,
    TranscriptSignal,
    TurnEnd,
)
from giljobe.pipeline.windowing import InlineExecutor, TurnWindower


class _ListSink:
    """수신 레코드를 그대로 모으는 인메모리 Sink(단언 표면)."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        self.records.append(record)


def _recorder(session_id: str = "iv-1"):
    sink = _ListSink()
    return SignalRecorder(session_id, [sink]), sink


# ─────────────────────────── window 레코드: nv+전사 병합 ───────────────────────────

def test_window_record_merges_nv_and_transcript_when_both_arrive():
    """nv·전사가 같은 t로 도착하면 둘을 한 window 레코드로 병합해 즉시 emit(실시간)."""
    rec, sink = _recorder()
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="hesitant", intensity=0.4, note="시선 아래"))
    assert sink.records == []  # 아직 전사 미도착 → 짝을 기다림(emit 없음)
    rec.on_signal(TranscriptSignal(t=1.5, window_s=3.0, text="음 제가"))

    assert len(sink.records) == 1
    r = sink.records[0]
    assert r["type"] == "window" and r["session_id"] == "iv-1"
    assert r["t"] == 1.5 and r["window_s"] == 3.0
    assert r["nonverbal"] == {"state": "hesitant", "intensity": 0.4, "note": "시선 아래"}
    assert r["transcript"] == "음 제가" and r["transcript_final"] is True


def test_window_record_merge_is_order_independent():
    """전사 먼저 와도(완료순) 같은 병합 결과 — 짝의 2번째 신호에서 1회 emit."""
    rec, sink = _recorder()
    rec.on_signal(TranscriptSignal(t=4.5, window_s=3.0, text="답변"))
    assert sink.records == []
    rec.on_signal(NonVerbalSignal(t=4.5, window_s=3.0, state="confident", intensity=0.7))
    assert len(sink.records) == 1
    assert sink.records[0]["transcript"] == "답변"
    assert sink.records[0]["nonverbal"]["state"] == "confident"


def test_nv_only_window_flushed_at_turn_end_with_empty_transcript():
    """오디오 없던 윈도우(전사 신호 없음)는 turn_end에서 부분 레코드로 flush(transcript=""·final=False)."""
    rec, sink = _recorder()
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="neutral", intensity=0.5))
    rec.on_signal(TurnEnd(t=3.0, transcript_full=""))

    window = [r for r in sink.records if r["type"] == "window"]
    assert len(window) == 1
    assert window[0]["nonverbal"]["state"] == "neutral"
    assert window[0]["transcript"] == "" and window[0]["transcript_final"] is False


def test_transcript_only_window_flushed_at_turn_end_with_null_nonverbal():
    """비디오 없던 윈도우(nv 신호 없음)는 turn_end에서 nonverbal=null, transcript_final=True로 flush."""
    rec, sink = _recorder()
    rec.on_signal(TranscriptSignal(t=1.5, window_s=3.0, text="소리만"))
    rec.on_signal(TurnEnd(t=3.0, transcript_full="소리만"))

    window = [r for r in sink.records if r["type"] == "window"]
    assert len(window) == 1
    assert window[0]["nonverbal"] is None
    assert window[0]["transcript"] == "소리만" and window[0]["transcript_final"] is True


def test_turn_end_flushes_leftover_partials_sorted_by_t_before_turn_end():
    """잔여 partial들은 turn_end 레코드보다 *먼저*, t-오름차순으로 flush된다."""
    rec, sink = _recorder()
    # 완료순(t 뒤섞임)으로 한 레인만 도착
    rec.on_signal(NonVerbalSignal(t=7.5, window_s=3.0, state="engaged", intensity=0.6))
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="nervous", intensity=0.3))
    rec.on_signal(TurnEnd(t=9.0, transcript_full="합본"))

    types = [r["type"] for r in sink.records]
    assert types == ["window", "window", "turn_end"]  # turn_end가 마지막
    assert [r["t"] for r in sink.records if r["type"] == "window"] == [1.5, 7.5]  # t-정렬


# ─────────────────────────── eval 레코드 (채널② 풀 평가) ───────────────────────────

def test_full_eval_signal_becomes_eval_record_with_center_t():
    """발화중 풀 EvaluationSignal → eval 레코드. t는 윈도우 중심(start+dur/2), eval 페이로드 중첩."""
    rec, sink = _recorder()
    sig = EvaluationSignal(
        window_start_s=0.0, window_dur_s=16.0,
        verbal={"logic": "ok"}, vocal={"pace": "fast"}, visual={"eye_contact": "weak"},
        critique=["근거 부족"], key_observations=["관찰1"], compact=False,
    )
    rec.on_signal(sig)

    assert len(sink.records) == 1
    r = sink.records[0]
    assert r["type"] == "eval" and r["session_id"] == "iv-1"
    assert r["t"] == 8.0 and r["window_s"] == 16.0  # [0,16) 중심
    assert r["eval"]["critique"] == ["근거 부족"] and r["eval"]["compact"] is False


# ─────────────────────────── turn_end 레코드 ───────────────────────────

def test_turn_end_record_carries_transcript_full_and_compact_eval():
    rec, sink = _recorder()
    compact = EvaluationSignal(
        window_start_s=16.0, window_dur_s=4.0, critique=["마무리 약함"], compact=True,
    )
    rec.on_signal(TurnEnd(t=20.0, transcript_full="제가 맡은 역할은 ...", eval=compact))

    r = sink.records[-1]
    assert r["type"] == "turn_end" and r["t"] == 20.0
    assert r["transcript_full"] == "제가 맡은 역할은 ..."
    assert r["eval"]["compact"] is True and r["eval"]["window_start_s"] == 16.0


def test_turn_end_record_null_eval_when_no_compact_tail():
    rec, sink = _recorder()
    rec.on_signal(TurnEnd(t=6.0, transcript_full="짧은 답"))
    assert sink.records[-1]["eval"] is None


# ─────────────────────────── 완료순 emit → 소비자가 t로 정렬 ───────────────────────────

def test_records_emitted_in_completion_order_carry_t_for_consumer_sort():
    """레코드는 완료순(시간순 아님)으로 나오지만 각자 t를 들고 있어 소비자가 t로 정렬 가능."""
    rec, sink = _recorder()
    # 늦은 윈도우가 먼저 완성(완료순)
    rec.on_signal(NonVerbalSignal(t=7.5, window_s=3.0, state="a", intensity=0.5))
    rec.on_signal(TranscriptSignal(t=7.5, window_s=3.0, text="C"))
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="b", intensity=0.5))
    rec.on_signal(TranscriptSignal(t=1.5, window_s=3.0, text="A"))

    emitted_t = [r["t"] for r in sink.records]
    assert emitted_t == [7.5, 1.5]  # 완료순(시간 역순)
    sorted_by_t = [r["transcript"] for r in sorted(sink.records, key=lambda r: r["t"])]
    assert sorted_by_t == ["A", "C"]  # 소비자가 t로 정렬하면 시간순 복원


def test_reset_discards_pending_partials_between_turns():
    rec, sink = _recorder()
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="x", intensity=0.5))  # 짝 없이 대기
    rec.reset()
    rec.on_signal(TurnEnd(t=3.0, transcript_full=""))  # reset으로 잔여 partial 폐기됨
    assert [r["type"] for r in sink.records] == ["turn_end"]  # window flush 없음


# ─────────────────────────── JSONL 유효성 (파일 라운드트립) ───────────────────────────

def test_jsonl_sink_writes_valid_lines_and_preserves_korean(tmp_path):
    path = tmp_path / "signals.jsonl"
    sink = JSONLSink(path)
    rec = SignalRecorder("iv-1", [sink])
    try:
        rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="hesitant", intensity=0.4, note="시선이 자주 아래로"))
        rec.on_signal(TranscriptSignal(t=1.5, window_s=3.0, text="음... 제가 맡았던 역할은"))
        rec.on_signal(TurnEnd(t=6.0, transcript_full="음... 제가 맡았던 역할은"))
    finally:
        sink.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # window 1 + turn_end 1
    parsed = [json.loads(ln) for ln in lines]  # 모든 라인이 유효 JSON
    assert parsed[0]["type"] == "window"
    assert parsed[0]["transcript"] == "음... 제가 맡았던 역할은"  # 한글 보존
    assert parsed[0]["nonverbal"]["note"] == "시선이 자주 아래로"
    assert parsed[1]["type"] == "turn_end"


# ─────────────────────────── SSE 프레임 포맷 ───────────────────────────

def test_sse_frame_format_and_newline_safety():
    r"""SSE 프레임은 ``data: {json}\n\n``. 전사에 개행이 있어도 json.dumps가 이스케이프해
    프레임이 깨지지 않는다(data 라인 1개)."""
    frame = sse_frame({"type": "window", "transcript": "첫 줄\n둘째 줄", "t": 1.5})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    # 페이로드(앞 'data: ' 6자, 뒤 '\n\n' 2자 제외)에는 raw 개행이 없어야 함(프레임 1개)
    payload = frame[len("data: "):-2]
    assert "\n" not in payload
    obj = json.loads(payload)  # 유효 JSON
    assert obj["transcript"] == "첫 줄\n둘째 줄"  # 개행은 데이터로 보존


def test_sse_sink_broadcasts_to_subscribers_and_drops_dead_ones():
    sink = SSESink()
    good: list[str] = []

    def good_cb(frame: str) -> None:
        good.append(frame)

    def dead_cb(frame: str) -> None:  # 끊긴 연결을 흉내
        raise ConnectionResetError

    sink.subscribe(good_cb)
    sink.subscribe(dead_cb)
    sink.emit({"type": "eval", "t": 8.0})
    assert len(good) == 1 and good[0].startswith("data: ")  # 정상 구독자는 받음

    sink.emit({"type": "eval", "t": 24.0})  # dead_cb는 1차 emit에서 제거됨
    assert len(good) == 2  # 정상 구독자는 계속 받음(끊긴 구독자가 막지 않음)


# ─────────────────── 배선: 실 TurnWindower → SignalRecorder → JSONLSink ───────────────────

def test_windower_to_recorder_to_jsonl_end_to_end(tmp_path):
    """emit이 실제 윈도워 신호 스트림을 직렬화하는지(InlineExecutor로 결정적). 3s 윈도우 3개의
    nv+전사 병합 window 레코드 + turn_end가 JSONL에 정확히 적힌다."""
    path = tmp_path / "out.jsonl"
    jsonl = JSONLSink(path)
    rec = SignalRecorder("iv-1", [jsonl])
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal, executor=InlineExecutor(),
    )
    try:
        t = 0.0
        while t < 9.0:  # [0,9) 1fps 프레임 + 0.5s 간격 PCM(nv·stt 둘 다 트리거)
            w.add_frame(t, b"jpeg")
            t += 1.0
        t = 0.0
        while t < 9.0:
            w.add_audio(t, b"pcm")
            t += 0.5
        w.poll(9.0)        # window [0,3),[3,6),[6,9) → 병합 레코드 3개 (eval 16s 미만이라 0개)
        w.end_turn(9.0)    # turn_end (compact tail [0,9) + transcript_full)
    finally:
        jsonl.close()
        w.close()

    parsed = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    windows = [r for r in parsed if r["type"] == "window"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]
    assert [r["t"] for r in windows] == [1.5, 4.5, 7.5]
    assert all(r["transcript"] == "조각" and r["transcript_final"] for r in windows)
    assert all(r["nonverbal"]["state"] == "attentive" for r in windows)
    assert len(turn_ends) == 1
    assert turn_ends[0]["transcript_full"] == "조각 조각 조각"
    assert turn_ends[0]["eval"]["compact"] is True


# ─────────── 적대적 리뷰 회귀: 트레일링 윈도우 nv 페어링 (실 스레드풀) ───────────

def _four_pools():
    return (
        concurrent.futures.ThreadPoolExecutor(max_workers=2),  # nv
        concurrent.futures.ThreadPoolExecutor(max_workers=2),  # stt
        concurrent.futures.ThreadPoolExecutor(max_workers=1),  # eval
        concurrent.futures.ThreadPoolExecutor(max_workers=1),  # tail
    )


def test_end_turn_trailing_window_nv_paired_not_orphaned_real_threadpool():
    """적대적 리뷰 회귀(robustness/join): end_turn 트레일링 부분윈도우의 *느린* nv가 TurnEnd emit
    전에 도착해 전사와 한 window 레코드로 병합된다. 과거 결함: end_turn이 nv를 안 기다려 nv가 늦게
    와 nonverbal=None 부분 레코드로 새고 nv read가 silent drop. InlineExecutor는 이 경로를 은폐하므로
    실 스레드풀 + 느린 nv(0.3s)로만 드러난다. nv_delay < eot_deadline라 결정적(타이밍 마진 아님)."""
    sink = _ListSink()
    rec = SignalRecorder("iv-1", [sink])
    nv_x, stt_x, eval_x, tail_x = _four_pools()
    w = TurnWindower(
        SlowMockCritic(nv_delay=0.3, eval_delay=0.0, transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal, eot_deadline_s=3.0,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        w.add_frame(0.5, b"jpeg")
        w.add_audio(0.5, b"pcm")
        w.add_audio(1.5, b"pcm")
        w.end_turn(2.0)  # 트레일링 [0,2): nv(느림)+stt(빠름) 제출 → 둘 다 대기 후 TurnEnd
    finally:
        w.close()
        for x in (nv_x, stt_x, eval_x, tail_x):
            x.shutdown(wait=False)

    windows = [r for r in sink.records if r["type"] == "window"]
    assert len(windows) == 1                      # 트레일링 윈도우 1개
    assert windows[0]["nonverbal"] is not None    # nv가 병합됨(과거 결함: None)
    assert windows[0]["nonverbal"]["state"] == "attentive"
    assert windows[0]["transcript"] == "조각" and windows[0]["transcript_final"] is True


# ─────────── 테스트 갭: 채널② eval 레코드 실 윈도워 e2e ───────────

def test_windower_full_eval_becomes_eval_record_end_to_end():
    """이 슬라이스 핵심 결정(A안) 회귀: 실 윈도워의 발화중 풀 평가(16s)가 eval 레코드로 직렬화된다.
    기존 e2e는 9s라 eval 레인 미발화였다 — 이 경로가 미검증이었음."""
    sink = _ListSink()
    rec = SignalRecorder("iv-1", [sink])
    w = TurnWindower(MockCritic(), on_signal=rec.on_signal, executor=InlineExecutor())
    try:
        t = 0.0
        while t < 18.0:  # 16s 윈도우가 완성되도록 18s까지
            w.add_frame(t, b"jpeg")
            t += 1.0
        t = 0.0
        while t < 18.0:
            w.add_audio(t, b"pcm")
            t += 0.5
        w.poll(18.0)  # eval [0,16) 발화 → EvaluationSignal(compact=False) → eval 레코드
    finally:
        w.close()

    evals = [r for r in sink.records if r["type"] == "eval"]
    assert len(evals) == 1
    assert evals[0]["t"] == 8.0 and evals[0]["window_s"] == 16.0  # [0,16) 중심
    assert evals[0]["eval"]["compact"] is False


# ─────────── 테스트 갭: 혼합 턴 cross-type t-정렬 (PLAN §4 계약) ───────────

def test_mixed_turn_cross_type_records_sort_chronologically_by_t():
    """PLAN §4 계약: window·eval·turn_end·partial이 완료순으로 섞여 나와도 t로 정렬하면 시간순 복원.
    t가 모든 레코드에서 '윈도우 중심'으로 일관(nv/stt center, eval center, turn_end=턴끝)임을 증명."""
    rec, sink = _recorder()
    rec.on_signal(NonVerbalSignal(t=4.5, window_s=3.0, state="engaged", intensity=0.6))
    rec.on_signal(TranscriptSignal(t=4.5, window_s=3.0, text="중간"))  # → window 병합(t=4.5) 즉시
    rec.on_signal(NonVerbalSignal(t=1.5, window_s=3.0, state="nervous", intensity=0.3))  # partial(짝 없음)
    rec.on_signal(EvaluationSignal(window_start_s=0.0, window_dur_s=16.0, compact=False))  # → eval(t=8.0)
    rec.on_signal(TurnEnd(
        t=20.0, transcript_full="...",
        eval=EvaluationSignal(window_start_s=16.0, window_dur_s=4.0, compact=True),
    ))

    assert sink.records[-1]["type"] == "turn_end"  # turn_end는 항상 마지막
    ordered = sorted(sink.records, key=lambda r: r["t"])
    assert [(r["type"], r["t"]) for r in ordered] == [
        ("window", 1.5), ("window", 4.5), ("eval", 8.0), ("turn_end", 20.0),
    ]  # t-정렬로 시간순 복원(partial·eval이 window 사이에 올바르게 끼어듦)


# ─────────── 동시성: 멀티스레드 on_signal 페어링(sink.py가 설계한 _lock 계약) ───────────

def test_concurrent_on_signal_pairs_without_loss_or_duplication():
    """sink.py docstring이 명시한 동시성 계약: 같은 t의 nv/tx가 서로 다른 워커 스레드에서 도착해도
    윈도우당 정확히 1 병합 레코드(_lock으로 setdefault+merge+del 원자성). 손실/중복 0."""
    out_lock = threading.Lock()

    class _LockedSink:  # _fanout이 lock 밖에서 부르므로 수집은 스레드안전하게
        def __init__(self) -> None:
            self.records: list[dict] = []

        def emit(self, record: dict) -> None:
            with out_lock:
                self.records.append(record)

    sink = _LockedSink()
    rec = SignalRecorder("iv-1", [sink])
    n = 24
    ts = [1.5 + 3.0 * i for i in range(n)]

    def send_nv(t: float) -> None:
        rec.on_signal(NonVerbalSignal(t=t, window_s=3.0, state="x", intensity=0.5))

    def send_tx(t: float) -> None:
        rec.on_signal(TranscriptSignal(t=t, window_s=3.0, text="t"))

    # nv/tx를 인터리브 제출해 같은 t 짝이 동시에 경합하도록
    jobs = []
    for t in ts:
        jobs.append((send_nv, t))
        jobs.append((send_tx, t))
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(fn, t) for fn, t in jobs]
        for f in futs:
            f.result()

    windows = [r for r in sink.records if r["type"] == "window"]
    assert len(windows) == n                              # 윈도우당 정확히 1개(중복/손실 없음)
    assert sorted(r["t"] for r in windows) == sorted(ts)  # 모든 t가 정확히 1회
    assert all(r["nonverbal"] is not None and r["transcript"] == "t" for r in windows)  # 전부 병합
