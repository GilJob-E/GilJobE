"""test_windowing — TurnWindower cadence/eot/race/레인독립 검증 (GPU/브라우저 불요).

InlineExecutor로 스케줄·합본·실패복구를 결정적으로, 실 스레드풀로 동시성(중복없음)·레인독립을
검증한다(PLAN §6). 더블은 Slice 3 산출물(MockCritic/MockTranscriber/SlowMockCritic)을 재사용하고,
실패/게이트가 필요한 케이스만 이 파일에 작은 로컬 더블을 둔다.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time

import pytest

from giljobe.analysis.mocks import MockCritic, MockTranscriber
from giljobe.models.signals import (
    EvaluationSignal,
    NonVerbalSignal,
    SentenceSignal,
    TranscriptSignal,
    TurnEnd,
)
from giljobe.pipeline.windowing import InlineExecutor, TurnWindower


def _fill(w: TurnWindower, *, dur: float, audio_step: float = 0.5) -> None:
    """[0,dur)에 1fps JPEG 프레임 + audio_step 간격 비어있지 않은 PCM을 채운다(합성).
    PCM이 있어야 stt 레인이 트리거된다."""
    t = 0.0
    while t < dur:
        w.add_frame(t, b"jpeg")
        t += 1.0
    t = 0.0
    while t < dur:
        w.add_audio(t, b"pcm")
        t += audio_step


def _by_type(sigs, cls):
    return [s for s in sigs if isinstance(s, cls)]


# ─────────────────────────── cadence (결정적, InlineExecutor) ───────────────────────────

def test_nv_stt_cadence_3s_and_eval_16s():
    """nv·stt는 3s 그리드, eval은 16s 그리드. nv·stt는 같은 (t, window_s)로 페어링."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=18.0)
    w.poll(18.0)

    nv = _by_type(sigs, NonVerbalSignal)
    tx = _by_type(sigs, TranscriptSignal)
    ev = _by_type(sigs, EvaluationSignal)

    # nv·stt 윈도우: [0,3),[3,6),[6,9),[9,12),[12,15),[15,18) → 중심 t 6개
    assert [s.t for s in nv] == [1.5, 4.5, 7.5, 10.5, 13.5, 16.5]
    assert all(s.window_s == 3.0 for s in nv)
    # stt 레인 1윈도우 1전사 — nv와 같은 t로 페어링
    assert [s.t for s in tx] == [1.5, 4.5, 7.5, 10.5, 13.5, 16.5]
    assert all(s.window_s == 3.0 and s.text for s in tx)
    # eval 16s 그리드: [0,16) 1개(=다음 32는 18 초과)
    assert [s.window_start_s for s in ev] == [0.0]
    assert ev[0].window_dur_s == 16.0 and ev[0].compact is False


def test_poll_idempotent_no_resubmit_on_repeated_poll():
    """같은 now로 poll을 여러 번 불러도 윈도우는 1회만 제출(counter는 제출 시 전진)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=9.0)
    w.poll(9.0)
    w.poll(9.0)  # 두 번째는 due 윈도우 없음
    w.poll(9.0)
    nv = _by_type(sigs, NonVerbalSignal)
    assert [s.t for s in nv] == [1.5, 4.5, 7.5]  # 3개, 중복 없음


def test_nv_skipped_without_frames_stt_skipped_without_audio():
    """비디오 없는 구간은 nv 스킵, 오디오 없는 구간은 stt 스킵(레인별 입력 요건)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    # 프레임만 [0,3), 오디오만 [3,6)
    w.add_frame(1.0, b"jpeg")
    w.add_audio(4.0, b"pcm")
    w.poll(6.0)
    nv = _by_type(sigs, NonVerbalSignal)
    tx = _by_type(sigs, TranscriptSignal)
    assert [s.t for s in nv] == [1.5]   # [3,6)은 프레임 없어 nv 스킵
    assert [s.t for s in tx] == [4.5]   # [0,3)은 오디오 없어 stt 스킵


# ─────────────────────── end_turn: transcript_full + compact tail ───────────────────────

def test_end_turn_emits_turn_end_with_transcript_full_and_compact_eval():
    sigs = []
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=sigs.append, executor=InlineExecutor(),
    )
    _fill(w, dur=20.0)  # tail [18,20)에도 오디오가 있게 20까지 채움(최종 stt flush 검증)
    w.poll(18.0)  # eval [0,16) 성공 → covered_until=16
    turn_end = w.end_turn(20.0)

    assert isinstance(turn_end, TurnEnd) and turn_end.t == 20.0
    # transcript_full = 모든 윈도우 전사 t-정렬 공백 join (poll 6개 + tail [18,20) 1개 = 7조각)
    assert turn_end.transcript_full == " ".join(["조각"] * 7)
    # compact eval tail: covered_until(16)~20 → embed (standalone emit 아님)
    assert turn_end.eval is not None and turn_end.eval.compact is True
    assert turn_end.eval.window_start_s == 16.0
    # turn_end도 on_signal로 emit됨
    assert any(isinstance(s, TurnEnd) for s in sigs)
    # compact tail은 standalone EvaluationSignal로 중복 emit되지 않음(turn_end.eval에만)
    standalone_compact = [s for s in _by_type(sigs, EvaluationSignal) if s.compact]
    assert standalone_compact == []


def test_end_turn_short_answer_whole_compact_tail():
    """풀 평가가 0개인 짧은 답변은 전체 구간을 compact tail로 1회 평가(whole_answer)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=6.0)
    w.poll(6.0)  # eval 윈도우 없음(16s 미만) → covered_until=0
    turn_end = w.end_turn(6.0)
    assert turn_end.eval is not None and turn_end.eval.compact is True
    assert turn_end.eval.window_start_s == 0.0  # 전체가 tail


def test_end_turn_empty_transcript_excluded_from_full():
    """빈 전사(무음) 조각은 transcript_full에서 제외(빈 PCM→빈 문자열)."""
    sigs = []
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="")),  # 항상 빈 전사
        on_signal=sigs.append, executor=InlineExecutor(),
    )
    _fill(w, dur=6.0)
    w.poll(6.0)
    turn_end = w.end_turn(6.0)
    assert turn_end.transcript_full == ""  # 빈 조각만 → 합본 빈 문자열


# ─────────────────── 실패 윈도우 → covered_until 정지 → tail 재커버 ───────────────────

class _FailFullEval(MockCritic):
    """지정 start의 풀 평가만 실패, compact tail은 성공. covered_until 연속 prefix·tail 재커버 검증용."""

    def __init__(self, fail_starts=(), **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_starts = set(fail_starts)

    def evaluate_window(self, jpeg_frames, pcm, *, window_start_s, window_dur_s, compact=False, **kw):
        if not compact and window_start_s in self.fail_starts:
            raise RuntimeError(f"full eval boom @ {window_start_s}")
        return super().evaluate_window(
            jpeg_frames, pcm, window_start_s=window_start_s, window_dur_s=window_dur_s, compact=compact, **kw
        )


def test_failed_full_eval_recovered_by_whole_compact_tail():
    """첫(유일) 풀 윈도우 실패 → covered_until 0 유지 → end_turn이 [0,now) 전체를 compact로 재커버."""
    sigs = []
    w = TurnWindower(_FailFullEval(fail_starts={0.0}), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=18.0)
    w.poll(18.0)  # eval [0,16) raise → covered_until 0, 풀 평가 emit 없음
    assert [s for s in _by_type(sigs, EvaluationSignal) if not s.compact] == []
    turn_end = w.end_turn(20.0)
    assert turn_end.eval is not None and turn_end.eval.compact is True
    assert turn_end.eval.window_start_s == 0.0  # 실패 구간을 tail이 재커버


def test_covered_until_contiguous_prefix_not_max():
    """[0,16) 실패 + [16,32) 성공 → covered_until은 32로 점프하지 않고 0에 멈춤(연속 prefix).
    max()였다면 32로 점프해 실패한 [0,16)을 영영 건너뛴다 → tail이 그 구멍을 재커버."""
    sigs = []
    w = TurnWindower(_FailFullEval(fail_starts={0.0}), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=34.0)
    w.poll(34.0)  # [0,16) 실패, [16,32) 성공
    turn_end = w.end_turn(34.0)
    # covered_until이 0이라 whole_answer → tail 시작이 0 (구멍을 건너뛰지 않음)
    assert turn_end.eval is not None and turn_end.eval.window_start_s == 0.0


def test_covered_until_advances_on_success_prefix():
    """[0,16) 성공 + [16,32) 실패 → covered_until=16 → tail은 [16,now)만 재커버."""
    sigs = []
    w = TurnWindower(_FailFullEval(fail_starts={16.0}), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=34.0)
    w.poll(34.0)  # [0,16) 성공 → covered_until=16; [16,32) 실패
    turn_end = w.end_turn(34.0)
    assert turn_end.eval is not None and turn_end.eval.window_start_s == 16.0


# ─────────────────── 동시성: 중복 없음 (실 스레드풀) ───────────────────

def test_concurrent_poll_no_duplicate_windows():
    """동시 poll(now)이 같은 윈도우를 두 번 제출하지 않는다(submit-후-전진 lock, race-free)."""
    w = TurnWindower(MockCritic())  # 자체 4레인 스레드풀
    try:
        _fill(w, dur=30.0)
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def hammer(_):
            barrier.wait()  # 동시 진입을 최대한 겹치게
            w.poll(30.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            list(ex.map(hammer, range(n_threads)))
        w.wait_idle(timeout=10.0)
        sigs = w.drain()

        nv = _by_type(sigs, NonVerbalSignal)
        tx = _by_type(sigs, TranscriptSignal)
        ev = _by_type(sigs, EvaluationSignal)
        # [0,3)..[27,30) → 10 윈도우, 각 정확히 1회(중복도 누락도 없음)
        assert sorted(s.t for s in nv) == [1.5, 4.5, 7.5, 10.5, 13.5, 16.5, 19.5, 22.5, 25.5, 28.5]
        assert len(nv) == 10 and len(tx) == 10
        assert len(ev) == 1  # eval [0,16) 1개
    finally:
        w.close()


# ─────────────────── 레인 독립성 (gate 기반 구조적 증명, 실 스레드풀) ───────────────────

class _GatedEvalCritic(MockCritic):
    """*풀* eval만 gate에 막혀 대기(compact tail은 통과). 풀 eval이 nv/stt·tail 레인을 막지
    않는지 구조적으로 증명하기 위한 더블 — compact까지 막으면 tail 레인 독립을 검증할 수 없다."""

    def __init__(self, gate: threading.Event, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = gate

    def evaluate_window(self, *args, compact: bool = False, **kwargs) -> EvaluationSignal:
        if not compact:  # 풀 평가만 블록(채널② 핫패스), compact tail은 즉시 통과
            self.gate.wait(timeout=5.0)
        return super().evaluate_window(*args, compact=compact, **kwargs)


def test_slow_eval_lane_does_not_block_nv_stt():
    """레인 독립성 핵심: gate에 막힌 풀 eval이 nv/stt 레인을 막지 않는다(별도 executor).
    벽시계 마진이 아니라 구조적으로: nv/stt 10개가 다 도착해도 eval은 *아직 미완*임을 확인.
    nv/stt가 eval 뒤로 직렬화됐다면 gate가 막혀 10개가 영영 안 모인다(2s 타임아웃에서 실패)."""
    gate = threading.Event()
    lock = threading.Lock()
    sigs = []

    def on_sig(s):
        with lock:
            sigs.append(s)

    nv_x = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    stt_x = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    eval_x = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    tail_x = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    w = TurnWindower(
        _GatedEvalCritic(gate), on_signal=on_sig,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=16.0)
        w.poll(16.0)  # nv·stt [0,3)..[12,15) = 5+5, eval [0,16) 1개(gate에 블록)

        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            with lock:
                n_fast = sum(isinstance(s, (NonVerbalSignal, TranscriptSignal)) for s in sigs)
            if n_fast >= 10:
                break
            time.sleep(0.01)

        with lock:
            n_fast = sum(isinstance(s, (NonVerbalSignal, TranscriptSignal)) for s in sigs)
            n_eval = sum(isinstance(s, EvaluationSignal) for s in sigs)
        assert n_fast == 10   # nv+stt 완료 — eval gate와 무관
        assert n_eval == 0    # eval은 아직 gate에 막혀 미완(구조적 증명)

        gate.set()
        w.wait_idle(timeout=5.0)
        with lock:
            n_eval2 = sum(isinstance(s, EvaluationSignal) for s in sigs)
        assert n_eval2 == 1   # gate 풀리면 eval도 완료
    finally:
        gate.set()
        w.close()
        for x in (nv_x, stt_x, eval_x, tail_x):
            x.shutdown(wait=False)


def test_end_turn_tail_lane_independent_of_blocked_eval():
    """compact tail은 전용 tail 레인이라, 진행중인 풀 eval이 eval 레인을 점유해도 안 밀린다.
    풀 eval을 gate로 막아둔 채 end_turn → tail이 즉시 돌아 turn_end.eval이 채워진다."""
    gate = threading.Event()
    lock = threading.Lock()
    sigs = []

    def on_sig(s):
        with lock:
            sigs.append(s)

    nv_x = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    stt_x = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    eval_x = concurrent.futures.ThreadPoolExecutor(max_workers=1)  # 단일 워커 — 막히면 후속 eval 큐잉
    tail_x = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    w = TurnWindower(
        _GatedEvalCritic(gate), on_signal=on_sig, eot_deadline_s=3.0,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=18.0)
        w.poll(18.0)  # 풀 eval [0,16) 제출 → gate에서 블록(eval 워커 점유)
        time.sleep(0.05)  # eval 워커가 잡을 시간
        turn_end = w.end_turn(20.0)  # tail은 별도 레인이라 eval 점유와 무관
        # tail 레인이 막히지 않았으므로 compact eval이 채워짐(eot_deadline 안)
        assert turn_end.eval is not None and turn_end.eval.compact is True
    finally:
        gate.set()
        w.close()
        for x in (nv_x, stt_x, eval_x, tail_x):
            x.shutdown(wait=False)


# ─────────────────── 턴 경계: reset / close ───────────────────

def test_reset_clears_buffers_schedule_and_transcripts():
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=6.0)
    w.poll(6.0)
    te1 = w.end_turn(6.0)
    assert te1.transcript_full  # 첫 턴 합본 있음

    w.reset()
    sigs.clear()
    # 새 턴: 카운터 3으로 리셋 → [0,3)부터 다시, 합본도 새로
    _fill(w, dur=6.0)
    w.poll(6.0)
    nv = _by_type(sigs, NonVerbalSignal)
    assert [s.t for s in nv] == [1.5, 4.5]  # _next_nv_end가 리셋됐음(이어 붙지 않음)
    te2 = w.end_turn(6.0)
    assert te2.t == 6.0


def test_close_drops_late_emits():
    """close() 후 제출된 추론의 emit은 _closed 가드로 no-op(on_signal도 안 불림)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    _fill(w, dur=6.0)
    w.close()
    w.poll(6.0)  # close 후 — InlineExecutor라 즉시 실행되지만 _emit이 버림
    assert sigs == []
    assert w.drain() == []


# ─────────────────── end_turn × 실 스레드풀: stt wait / 타임아웃 (적대적 리뷰 보강) ───────────────────

class _SlowSttCritic(MockCritic):
    """transcribe_window에만 sleep을 얹은 더블 — 실 스레드풀에서 end_turn이 stt를 *기다리는지/
    데드라인에 끊는지* 검증용. nv/eval/compact-tail은 즉시(stt 레인만 지연)."""

    def __init__(self, *, stt_delay: float = 0.2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stt_delay = stt_delay

    def transcribe_window(self, *args, **kwargs) -> str:
        time.sleep(self.stt_delay)
        return super().transcribe_window(*args, **kwargs)


def _four_pools():
    return (
        concurrent.futures.ThreadPoolExecutor(max_workers=2),  # nv
        concurrent.futures.ThreadPoolExecutor(max_workers=4),  # stt
        concurrent.futures.ThreadPoolExecutor(max_workers=2),  # eval
        concurrent.futures.ThreadPoolExecutor(max_workers=1),  # tail
    )


def test_end_turn_waits_for_async_stt_before_assembling_transcript_full():
    """실 스레드풀에서 end_turn이 transcript_full 합본 *전에* 비동기 stt를 기다린다(InlineExecutor가
    감추는 경로). 느린 stt(0.2s)라도 모든 윈도우 전사가 합본에 들어와야 한다(기다림의 증거).
    하한 타이밍만 검증(상한은 부하 플레이크라 안 둠 — Slice 3 교훈)."""
    sigs = []
    nv_x, stt_x, eval_x, tail_x = _four_pools()
    w = TurnWindower(
        _SlowSttCritic(stt_delay=0.2, transcriber=MockTranscriber(text="조각")),
        on_signal=sigs.append, eot_deadline_s=3.0,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=9.0)
        w.poll(9.0)  # stt [0,3),[3,6),[6,9) 비동기 제출(각 0.2s sleep)
        t0 = time.perf_counter()
        turn_end = w.end_turn(9.0)
        elapsed = time.perf_counter() - t0
        # 합본 완전성 = end_turn이 비동기 stt를 끝까지 기다렸다는 증거(안 기다렸으면 부분/빈 합본)
        assert turn_end.transcript_full == " ".join(["조각"] * 3)
        assert elapsed >= 0.15  # 느린 stt(0.2s)를 기다렸으니 즉시 반환 아님(하한만)
    finally:
        w.close()
        for x in (nv_x, stt_x, eval_x, tail_x):
            x.shutdown(wait=False)


def test_end_turn_stt_timeout_warns_and_is_best_effort(caplog):
    """stt가 eot_deadline을 못 맞추면 end_turn은 데드라인에 끊고(턴이 느린 stt에 안 막힘) 부분
    transcript_full을 내되 *경고를 로깅*한다(silent drop 방지). compact eval은 전용 레인이라 그대로."""
    sigs = []
    nv_x, stt_x, eval_x, tail_x = _four_pools()
    w = TurnWindower(
        _SlowSttCritic(stt_delay=0.5, transcriber=MockTranscriber(text="조각")),
        on_signal=sigs.append, eot_deadline_s=0.1,  # stt(0.5s) >> deadline(0.1s)
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=6.0)
        w.poll(6.0)  # stt [0,3),[3,6) 제출(각 0.5s)
        t0 = time.perf_counter()
        with caplog.at_level(logging.WARNING, logger="giljobe.pipeline.windowing"):
            turn_end = w.end_turn(6.0)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.4               # 데드라인(0.1s)에 끊겨 0.5s stt에 안 막힘
        assert turn_end.transcript_full == ""  # stt 미완 → 부분(여기선 빈) 합본
        assert turn_end.eval is not None and turn_end.eval.compact is True  # tail은 별도 레인
        assert any("stt" in r.message for r in caplog.records)  # silent drop 아님 — 경고 로깅
    finally:
        w.close()
        for x in (nv_x, stt_x, eval_x, tail_x):
            x.shutdown(wait=False)


def test_partial_executor_injection_rejected():
    """레인 executor 부분 주입은 ValueError로 막는다(None.submit() AttributeError 사전 차단)."""
    only_nv = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(ValueError, match="부분 주입"):
            TurnWindower(MockCritic(), nv_executor=only_nv)  # stt/eval/tail 누락
    finally:
        only_nv.shutdown(wait=False)


def test_late_video_frames_caught_up_after_window_close():
    """★회귀(NV 레인 버그, giljob-docker qa/nv-lane-investigation.md): 비디오가 GOP 버스트·
    네트워크 지터로 윈도우 마감 *후* 도착하면 마감 시점 슬라이스가 비어 NV가 영구 스킵됐다
    (kor 실턴 13/13 nonverbal=null). 늦게 도착한 프레임은 다음 poll/end_turn에서 catch-up
    제출돼야 한다 — 늦은 NV가 안 하는 것보다 낫다(레코드는 t 자기기술, 소비자가 정렬)."""
    sigs = []
    w = TurnWindower(MockCritic(), on_signal=sigs.append, executor=InlineExecutor())
    t = 0.0
    while t < 9.0:  # 오디오는 실시간 도착(현실: 오디오는 부드럽고 비디오만 버스트)
        w.add_audio(t, b"pcm")
        t += 0.5
    w.poll(3.2)                    # [0,3) 마감 — 프레임 미도착(버스트 지연)
    w.add_frame(0.5, b"jpeg")      # [0,3) 프레임이 늦게 도착
    w.add_frame(1.5, b"jpeg")
    w.poll(6.2)                    # [3,6) 마감 + [0,3) catch-up 기대
    w.add_frame(4.0, b"jpeg")      # [3,6) 프레임 늦게 도착
    w.end_turn(9.0)                # end_turn에서도 catch-up 기대

    nv_ts = [s.t for s in _by_type(sigs, NonVerbalSignal)]
    assert 1.5 in nv_ts, "poll catch-up: 마감 후 도착한 [0,3) 프레임으로 NV read"
    assert 4.5 in nv_ts, "end_turn catch-up: [3,6) 회수"
    # 전사는 마감 시점에 윈도우당 정확히 1회 — catch-up이 stt를 재제출하면 안 된다
    tx_ts = sorted(s.t for s in _by_type(sigs, TranscriptSignal))
    assert tx_ts == [1.5, 4.5, 7.5]


def test_external_transcript_mode_sentence_lane_replaces_grids():
    """외부 전사 모드(PR #13 sideband): nv·stt 3s 그리드 OFF — sideband 이벤트가 문장 경계를
    만들고, 문장 단위로 4채널(SentenceSignal)이 나온다. end_turn은 미완 델타를 flush로 회수하고
    문장 텍스트를 t-정렬해 transcript_full로 합본한다(gemma 전사 호출 0)."""
    sigs = []
    w = TurnWindower(
        MockCritic(), on_signal=sigs.append, executor=InlineExecutor(),
        transcript_source="external",
    )
    pcm = b"\x00\x01" * 800  # Int16 짝수 길이 — 경계 스냅 경로가 실제 디코드한다
    t = 0.0
    while t < 12.0:
        w.add_frame(t, b"jpeg")
        w.add_audio(t, pcm)
        t += 0.5
    w.poll(6.0)  # 내부 모드라면 nv/tx가 났을 시점 — 그리드 비활성 확인
    assert _by_type(sigs, NonVerbalSignal) == []
    assert _by_type(sigs, TranscriptSignal) == []

    out = w.ingest_realtime_event(
        {"eventKind": "transcript.completed",
         "detail": {"transcript": "첫 문장입니다. 둘째 문장.", "itemId": "i1"}},
        media_now=6.0,
    )
    assert out["accepted"] is True and out["sentences"] == 2
    sents = _by_type(sigs, SentenceSignal)
    assert [s.text for s in sents] == ["첫 문장입니다.", "둘째 문장."]
    assert sents[0].nonverbal is not None  # 프레임 슬라이스 → nv read 수행(MockCritic)
    assert sents[0].source == "punct"

    # 메타데이터 전용 이벤트(현 PR13 중계)도 수용 — 텍스트 없는 발화 경계
    meta = w.ingest_realtime_event({"eventKind": "transcript.completed"}, media_now=8.5)
    assert meta["accepted"] is True

    # 미완 델타 → end_turn flush가 마지막 문장으로 회수 + 합본
    w.ingest_realtime_event(
        {"type": "analysis.transcript.delta", "detail": {"transcript": "잔여 조각", "itemId": "i2"}},
        media_now=10.0,
    )
    te = w.end_turn(12.0)
    sents = _by_type(sigs, SentenceSignal)
    assert sents[-1].source == "flush" and sents[-1].text == "잔여 조각"
    assert te.transcript_full == "첫 문장입니다. 둘째 문장. 잔여 조각"


def test_sentence_eval_grid_count_then_span_trigger_and_tail_chain():
    """문장 정렬 eval(eval_grid="sentence"): 16s 벽시계 그리드 OFF — 앵커 이후 문장 4개(count)
    또는 ~15s(span) 누적 시 [앵커, 마지막 문장 끝)을 풀 평가. 윈도우가 문장 경계에 정렬되고,
    가변 폭 covered_until 연쇄로 end_turn tail은 마지막 평가 끝부터 시작한다."""
    sigs = []
    w = TurnWindower(
        MockCritic(), on_signal=sigs.append, executor=InlineExecutor(),
        transcript_source="external", eval_grid="sentence",
    )
    pcm = b"\x00\x01" * 800
    t = 0.0
    while t < 40.0:
        w.add_frame(t, b"jpeg")
        w.add_audio(t, pcm)
        t += 0.5
    w.poll(17.0)  # 벽시계 그리드였다면 [0,16) eval이 났을 시점
    assert _by_type(sigs, EvaluationSignal) == []

    # count 트리거: 문장 4개 → [0(초기 앵커), 마지막 문장 끝)
    w.ingest_realtime_event(
        {"eventKind": "transcript.completed",
         "detail": {"transcript": "하나입니다. 둘입니다. 셋입니다. 넷입니다.", "itemId": "i1"}},
        media_now=10.0,
    )
    evs = _by_type(sigs, EvaluationSignal)
    assert len(evs) == 1 and evs[0].window_start_s == 0.0
    first_end = evs[0].window_start_s + evs[0].window_dur_s

    # span 트리거: 문장 1개라도 앵커에서 15s 이상 경과 → [직전 윈도우 끝, 이 문장 끝) — 앵커 연쇄
    w.ingest_realtime_event(
        {"eventKind": "transcript.completed",
         "detail": {"transcript": "한참 뒤의 다섯째 문장입니다.", "itemId": "i2"}},
        media_now=30.0,
    )
    evs = _by_type(sigs, EvaluationSignal)
    assert len(evs) == 2
    assert evs[1].window_start_s == pytest.approx(first_end)

    # 가변 폭 윈도우에서도 covered_until이 연쇄 → tail은 둘째 윈도우 끝부터(이중 평가 없음)
    te = w.end_turn(40.0)
    assert te.eval is not None and te.eval.compact is True
    assert te.eval.window_start_s == pytest.approx(
        evs[1].window_start_s + evs[1].window_dur_s)
    # end_turn flush 문장은 풀 평가를 추가로 내지 않는다(allow_eval=False)
    assert len([s for s in _by_type(sigs, EvaluationSignal) if not s.compact]) == 2


def test_internal_mode_rejects_realtime_events():
    """내부 전사 모드(기본)는 sideband 이벤트를 정중히 무시 — 기존 동작 무변경 가드."""
    w = TurnWindower(MockCritic(), executor=InlineExecutor())
    out = w.ingest_realtime_event({"eventKind": "transcript.completed"}, media_now=3.0)
    assert out == {"accepted": False, "reason": "internal_transcript_mode"}
