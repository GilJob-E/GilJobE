"""test_e2e_mock — 합성 미디어 → 풀 파이프라인 → JSONL 배선 증명 (GPU/브라우저 불요).

이 슬라이스(§7-6)의 목적은 **aiortc·모델만 빼고 나머지 전부를 실제로 배선**해, 합성 JPEG/PCM이
들어가면 소비자(면접관 LLM)가 먹을 JSONL 레코드가 나온다는 것을 한 흐름으로 증명하는 것이다:

    합성 프레임/PCM → 실 TurnWindower(실 4레인 스레드풀) → MockCritic/MockTranscriber
                    → SignalRecorder(조인+fan-out) → JSONLSink(+SSESink) → 레코드

Slice 4/5의 단위·배선 테스트는 InlineExecutor(동기·결정적)로 스케줄·합본·병합 *로직*을 봤다.
여기서는 **실 스레드풀**로 돌려, InlineExecutor가 감추는 것들을 e2e로 증명한다(이 파일의 모든
테스트는 실 스레드풀이 load-bearing — InlineExecutor면 완료순이 안 보이거나, gate 테스트는 poll에서
교착한다):
  ① 완료순 emit — 윈도우가 t-순서가 아니라 *완성 순서*로 적히고, 소비자가 t로 정렬하면 시간순 복원.
  ② 풀 eval 레인 독립성 — 막힌 풀 평가가 nv/stt window 레코드를 막지 않고 둘 다 JSONL까지 간다.
  ③ 한-레인-only 윈도우의 turn_end partial flush가 실 풀의 poll-skip→nv/stt-wait→sweep을 거쳐 JSONL로.
  ④ 멀티턴(reset)·멀티싱크(JSONL+SSE fan-out)의 전체 배선.

더블은 Slice 3 산출물(MockCritic/MockTranscriber)을 재사용하고, 완료순을 뒤섞는 가변지연 더블과
풀 eval을 막는 gate 더블만 이 파일에 작게 둔다(타이밍·게이트가 필요한 케이스만 로컬 더블 — windowing
테스트 관례).
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time

from giljobe.analysis.mocks import MockCritic, MockTranscriber
from giljobe.emit.sink import JSONLSink, SignalRecorder, SSESink
from giljobe.models.signals import EvaluationSignal
from giljobe.pipeline.windowing import TurnWindower


# ─────────────────────────── 합성 입력 + 헬퍼 ───────────────────────────

def _fill(w: TurnWindower, *, dur: float, audio_step: float = 0.5) -> None:
    """[0,dur)에 1fps JPEG 프레임 + audio_step 간격 비어있지 않은 PCM을 채운다(합성).
    nv는 프레임, stt는 PCM이 있어야 트리거된다(둘 다 채워 윈도우별 nv·전사 페어 생성)."""
    t = 0.0
    while t < dur:
        w.add_frame(t, b"jpeg")
        t += 1.0
    t = 0.0
    while t < dur:
        w.add_audio(t, b"pcm")
        t += audio_step


def _real_pools():
    """실 4레인 스레드풀(nv/stt/eval/tail). 워커를 넉넉히 줘 한 턴의 윈도우들이 *동시에* 돌게
    한다 — 완료순이 큐잉이 아니라 실제 추론 지연으로 결정되도록(가변지연 테스트의 전제)."""
    return (
        concurrent.futures.ThreadPoolExecutor(max_workers=4),  # nv
        concurrent.futures.ThreadPoolExecutor(max_workers=4),  # stt
        concurrent.futures.ThreadPoolExecutor(max_workers=2),  # eval
        concurrent.futures.ThreadPoolExecutor(max_workers=1),  # tail
    )


def _shutdown(*pools) -> None:
    # wait=False라도 안전: w.close()를 먼저 불러 _closed 가드가 늦은 emit을 no-op으로 만들고,
    # Mock은 즉시 끝나는 유한 작업이라 orphaned 워커가 다음 테스트로 새지 않는다(windowing 테스트 관례).
    for p in pools:
        p.shutdown(wait=False)


def _read_jsonl(path) -> list[dict]:
    """JSONL 파일의 모든 라인을 파싱(라인 = 레코드 1개, 유효 JSON임을 동시에 검증)."""
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


class _LockedListSink:
    """인메모리 Sink — 워커 스레드들에서 동시 emit되므로 수집을 lock으로 보호한다.
    JSONL을 닫기 전(턴 진행 중)에 레코드 도착을 관찰해야 하는 게이트 테스트용 단언 표면."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, record: dict) -> None:
        with self._lock:
            self.records.append(record)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.records)


# ─────────────── 1. 단일 턴: 실 스레드풀 풀 파이프라인 → JSONL ───────────────

def test_full_pipeline_single_turn_real_threadpool_to_jsonl(tmp_path):
    """합성 9s 턴이 실 4레인 스레드풀을 거쳐 JSONL에 정확한 레코드로 떨어진다(전체 배선 스모크).
    내용 단언은 타이밍 무관(end_turn이 nv/stt/tail을 기다리므로): window 3개(nv+전사 병합) +
    turn_end 1개(transcript_full 합본 + compact eval), turn_end는 파일 마지막."""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    rec = SignalRecorder("iv-1", [jsonl])
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=9.0)
        w.poll(9.0)        # 윈도우 [0,3),[3,6),[6,9) → nv+전사 병합 레코드 3개(eval 16s 미만→0)
        w.end_turn(9.0)    # nv/stt/tail 대기 후 turn_end(transcript_full + compact tail)
    finally:
        w.close()          # _closed 가드 → 이후 jsonl.close() 안전(닫힌 파일에 늦은 emit 안 함)
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    parsed = _read_jsonl(path)
    windows = [r for r in parsed if r["type"] == "window"]
    turn_ends = [r for r in parsed if r["type"] == "turn_end"]

    assert len(windows) == 3 and len(turn_ends) == 1
    # 완료순이라 파일 순서는 보장 안 됨 → t로 정렬해 시간순 복원 후 단언(소비자 계약).
    by_t = sorted(windows, key=lambda r: r["t"])
    assert [r["t"] for r in by_t] == [1.5, 4.5, 7.5]
    assert all(r["session_id"] == "iv-1" and r["window_s"] == 3.0 for r in by_t)
    assert all(r["nonverbal"]["state"] == "attentive" for r in by_t)       # nv 병합됨
    assert all(r["transcript"] == "조각" and r["transcript_final"] for r in by_t)  # 전사 병합됨
    # turn_end: end_turn이 모든 nv/stt를 기다린 뒤 emit하므로 항상 파일 마지막.
    assert parsed[-1]["type"] == "turn_end"
    assert turn_ends[0]["transcript_full"] == "조각 조각 조각"  # 3 윈도우 t-정렬 합본
    assert turn_ends[0]["eval"]["compact"] is True            # compact tail 임베드


# ─────────────── 2. 완료순 emit → 소비자 t-정렬 복원 (가변지연, 실 스레드풀) ───────────────

class _VariableLatencyCritic(MockCritic):
    """윈도우(t)마다 nv·stt 지연이 다른 더블 — 이른 t일수록 *더 느리게* 만들어, 늦은 t 윈도우가
    먼저 완성된다. 그러면 완료순 emit이 t-순서와 어긋나(파일은 t 역순) 소비자의 t-정렬 계약을
    '이빨 있게' 검증할 수 있다. 0.1s 단위 지연차가 윈도우 간 완성 순서를 가른다(스케줄 지터보다 큼)."""

    @staticmethod
    def _delay_for(t: float) -> float:
        return 0.30 if t < 3.0 else 0.20 if t < 6.0 else 0.10

    def read_nonverbal(self, *args, t: float, **kwargs):
        time.sleep(self._delay_for(t))
        return super().read_nonverbal(*args, t=t, **kwargs)

    def transcribe_window(self, *args, t: float, **kwargs):
        time.sleep(self._delay_for(t))
        return super().transcribe_window(*args, t=t, **kwargs)


def test_completion_order_emit_restored_by_consumer_t_sort(tmp_path):
    """PLAN §6 핵심 리스크 e2e: 윈도우가 *완료순*(t-순서 아님)으로 JSONL에 적히고, 각 레코드가
    t로 self-describing이라 소비자가 t로 정렬하면 시간순이 복원된다. 가변지연으로 7.5(빠름)가
    1.5(느림)보다 먼저 적힘을 직접 확인 → 완료순 emit이 진짜 일어남(InlineExecutor면 안 보임)."""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    rec = SignalRecorder("iv-1", [jsonl])
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        _VariableLatencyCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal, eot_deadline_s=3.0,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=9.0)
        w.poll(9.0)
        w.end_turn(9.0)
    finally:
        w.close()
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    parsed = _read_jsonl(path)
    raw_window_ts = [r["t"] for r in parsed if r["type"] == "window"]

    assert sorted(raw_window_ts) == [1.5, 4.5, 7.5]            # 세 윈도우 모두 존재(손실 없음)
    assert raw_window_ts != [1.5, 4.5, 7.5]                    # 파일은 t-정렬이 아님(완료순)
    assert raw_window_ts.index(7.5) < raw_window_ts.index(1.5)  # 빠른 윈도우가 먼저 적힘
    # 소비자가 t로 정렬하면 시간순 복원 + 내용 보존.
    chrono = sorted((r for r in parsed if r["type"] == "window"), key=lambda r: r["t"])
    assert [r["t"] for r in chrono] == [1.5, 4.5, 7.5]
    assert all(r["transcript"] == "조각" and r["nonverbal"]["state"] == "attentive" for r in chrono)
    # turn_end는 완료순과 무관하게 항상 마지막(end_turn이 nv/stt 다 기다린 뒤 emit).
    assert parsed[-1]["type"] == "turn_end"
    assert parsed[-1]["transcript_full"] == "조각 조각 조각"


# ─────────────── 3. 풀 eval 레인 독립성 → eval 레코드 JSONL (gate, 실 스레드풀) ───────────────

class _GatedFullEvalCritic(MockCritic):
    """발화중 *풀* 평가만 gate에 막고 compact tail은 통과시키는 더블. 풀 eval이 별도 레인이라
    nv/stt 레인을 막지 않음을 *구조적으로* 증명한다(test_windowing 게이트 패턴을 emit/JSONL까지 확장).
    InlineExecutor였다면 poll()이 이 gate.wait에서 교착하므로, 이 테스트는 실 스레드풀이 load-bearing."""

    def __init__(self, gate: threading.Event, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = gate

    def evaluate_window(self, *args, compact: bool = False, **kwargs) -> EvaluationSignal:
        if not compact:  # 풀 평가만 블록(채널② 핫패스), compact tail은 즉시 통과
            self.gate.wait(timeout=5.0)
        return super().evaluate_window(*args, compact=compact, **kwargs)


def test_full_eval_lane_independence_to_jsonl(tmp_path):
    """긴 턴(18s)에서 막힌 풀 eval이 nv/stt window 레코드를 막지 않고(레인 독립성), gate를 풀면
    풀 eval(채널② A안)이 eval 레코드로 JSONL까지 직렬화된다. 벽시계 마진이 아니라 구조적 증명:
    eval이 gate에 막힌 동안 window 6개는 다 떨어지되 eval 레코드는 0개. (nv/stt가 eval 뒤로
    직렬화됐다면 6개가 영영 안 모여 2s 타임아웃에서 실패.)"""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    listed = _LockedListSink()
    rec = SignalRecorder("iv-1", [jsonl, listed])
    gate = threading.Event()
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        _GatedFullEvalCritic(gate, transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal, eot_deadline_s=3.0,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=18.0)
        w.poll(18.0)  # nv·stt 6윈도우 + 풀 eval [0,16)(gate에 블록)

        # 레인 독립성: 풀 eval이 막혀 있어도 nv/stt 6 window 레코드는 다 떨어진다(별도 레인).
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            if sum(r["type"] == "window" for r in listed.snapshot()) >= 6:
                break
            time.sleep(0.01)
        recs = listed.snapshot()
        assert sum(r["type"] == "window" for r in recs) == 6   # nv/stt 완료 — eval gate와 무관
        assert sum(r["type"] == "eval" for r in recs) == 0     # eval은 아직 gate에 막혀 미발화

        gate.set()
        w.wait_idle(timeout=5.0)  # gate 풀리면 풀 eval 완료 → eval 레코드 emit
        assert sum(r["type"] == "eval" for r in listed.snapshot()) == 1

        w.end_turn(18.0)
    finally:
        gate.set()  # 단언 실패로 빠져도 gate.wait가 영영 안 막히게
        w.close()
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    parsed = _read_jsonl(path)
    windows = [r for r in parsed if r["type"] == "window"]
    evals = [r for r in parsed if r["type"] == "eval"]

    assert len(windows) == 6
    assert sorted(r["t"] for r in windows) == [1.5, 4.5, 7.5, 10.5, 13.5, 16.5]
    assert len(evals) == 1
    assert evals[0]["t"] == 8.0 and evals[0]["window_s"] == 16.0  # [0,16) 중심
    assert evals[0]["eval"]["compact"] is False                  # 발화중 풀 평가
    assert parsed[-1]["type"] == "turn_end"
    assert parsed[-1]["transcript_full"] == " ".join(["조각"] * 6)
    assert parsed[-1]["eval"]["compact"] is True                 # end-of-turn compact tail


# ─────────────── 4. 멀티턴: reset로 턴 격리 → 2 turn_end, 누수 없음 (실 스레드풀) ───────────────

def test_multi_turn_with_reset_isolates_turns_in_jsonl(tmp_path):
    """공유 윈도워/recorder를 reset으로 두 턴 진행 → 한 JSONL 파일에 두 턴이 turn_end로 구분돼
    적힌다. 두 턴이 같은 t 그리드를 재사용(reset이 되감음)하므로, 파일을 turn_end 경계로 분절해
    각 턴의 윈도우 수·합본을 검증한다(턴 간 신호 누수 없음 = 분절별 정확한 개수)."""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    rec = SignalRecorder("iv-1", [jsonl])
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        # 턴 1 — 6s(윈도우 2개)
        _fill(w, dur=6.0)
        w.poll(6.0)
        w.end_turn(6.0)
        # 턴 경계: 레인 idle(end_turn이 nv/stt/tail 대기 완료) → reset 안전(윈도워+recorder 대칭)
        w.reset()
        rec.reset()
        # 턴 2 — 9s(윈도우 3개)
        _fill(w, dur=9.0)
        w.poll(9.0)
        w.end_turn(9.0)
    finally:
        w.close()
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    parsed = _read_jsonl(path)
    te_idx = [i for i, r in enumerate(parsed) if r["type"] == "turn_end"]
    assert len(te_idx) == 2  # 두 턴 = turn_end 2개

    seg1 = parsed[: te_idx[0] + 1]   # 턴 1: 첫 turn_end까지(포함)
    seg2 = parsed[te_idx[0] + 1 :]   # 턴 2: 그 뒤 전부
    w1 = [r for r in seg1 if r["type"] == "window"]
    w2 = [r for r in seg2 if r["type"] == "window"]

    assert len(w1) == 2 and sorted(r["t"] for r in w1) == [1.5, 4.5]      # 턴1 윈도우 2개
    assert seg1[-1]["transcript_full"] == "조각 조각"                     # 턴1 합본(2 조각)
    assert len(w2) == 3 and sorted(r["t"] for r in w2) == [1.5, 4.5, 7.5]  # 턴2 윈도우 3개(누수 없음)
    assert seg2[-1]["type"] == "turn_end" and seg2[-1]["transcript_full"] == "조각 조각 조각"


# ─────────────── 5. 멀티싱크 fan-out: JSONL + SSE가 동일 레코드 스트림 수신 ───────────────

def test_recorder_fans_out_identical_records_to_jsonl_and_sse(tmp_path):
    """PLAN §4 송출 설계 배선: SignalRecorder가 같은 레코드 스트림을 감사로그(JSONL)와 라이브
    스트림(SSE) 양쪽에 fan-out한다. 실 스레드풀이라 싱크별 *순서*는 다를 수 있으나(완료순 인터리브)
    *멀티셋*은 동일해야 한다(어느 싱크도 레코드를 잃거나 더 받지 않음)."""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    sse = SSESink()
    sse_frames: list[str] = []
    sse_lock = threading.Lock()

    def collect(frame: str) -> None:  # server 슬라이스의 StreamResponse.write 자리 — 여기선 수집
        with sse_lock:  # fan-out이 워커 스레드들에서 동시 호출되므로 수집은 lock 보호
            sse_frames.append(frame)

    sse.subscribe(collect)
    rec = SignalRecorder("iv-1", [jsonl, sse])
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        _fill(w, dur=9.0)
        w.poll(9.0)
        w.end_turn(9.0)
    finally:
        w.close()
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    jsonl_records = _read_jsonl(path)
    with sse_lock:
        sse_records = [json.loads(f[len("data: "):-2]) for f in sse_frames]  # data: {json}\n\n

    # 둘 다 window 3 + turn_end 1 = 4 레코드(비지 않음 — 동일 비교가 공허하지 않게).
    assert len(jsonl_records) == 4 and len(sse_records) == 4

    def _multiset(records):  # 순서 무관 비교(완료순 인터리브로 싱크별 순서 다를 수 있음)
        return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in records)

    assert _multiset(jsonl_records) == _multiset(sse_records)  # 동일 레코드 스트림(손실/중복 0)
    assert sum(r["type"] == "turn_end" for r in sse_records) == 1  # turn_end도 SSE로 송출됨


# ─────────────── 6. 한-레인-only 윈도우: turn_end partial flush → JSONL (실 스레드풀) ───────────────

def test_one_lane_only_windows_flushed_as_partials_to_jsonl(tmp_path):
    """프레임만/오디오만 있는 윈도우가 실 풀의 poll-skip → end_turn nv/stt-wait → recorder sweep을
    거쳐 turn_end *앞에* t-정렬 partial 레코드로 JSONL에 떨어진다(신호 손실 0). Slice 5는 이 sweep을
    hand-built 신호로만 봤다 — 여기선 실 윈도워가 한 레인을 스킵하고 늦은 신호를 끝까지 기다리는
    배선(트레일링-nv silent drop이 났던 그 경로)을 e2e로 증명한다."""
    path = tmp_path / "signals.jsonl"
    jsonl = JSONLSink(path)
    rec = SignalRecorder("iv-1", [jsonl])
    nv_x, stt_x, eval_x, tail_x = _real_pools()
    w = TurnWindower(
        MockCritic(transcriber=MockTranscriber(text="조각")),
        on_signal=rec.on_signal,
        nv_executor=nv_x, stt_executor=stt_x, eval_executor=eval_x, tail_executor=tail_x,
    )
    try:
        # 윈도우 [0,3): 프레임만(오디오 없음) → nv 제출, stt 스킵
        w.add_frame(0.0, b"jpeg")
        w.add_frame(1.0, b"jpeg")
        w.add_frame(2.0, b"jpeg")
        # 윈도우 [3,6): 오디오만(프레임 없음) → stt 제출, nv 스킵
        w.add_audio(3.0, b"pcm")
        w.add_audio(3.5, b"pcm")
        w.add_audio(4.0, b"pcm")
        w.poll(6.0)        # nv@[0,3) 만, stt@[3,6) 만 제출(짝 없는 한 레인)
        w.end_turn(6.0)    # nv/stt 대기 → recorder가 짝 못 채운 윈도우를 turn_end에서 sweep
    finally:
        w.close()
        _shutdown(nv_x, stt_x, eval_x, tail_x)
        jsonl.close()

    parsed = _read_jsonl(path)
    windows = [r for r in parsed if r["type"] == "window"]
    by_t = {r["t"]: r for r in windows}

    assert sorted(by_t) == [1.5, 4.5]          # 두 한-레인 윈도우 모두 partial로 flush(손실 없음)
    # [0,3): 프레임만 → nv 있음, 전사 없음(stt 스킵)
    assert by_t[1.5]["nonverbal"]["state"] == "attentive"
    assert by_t[1.5]["transcript"] == "" and by_t[1.5]["transcript_final"] is False
    # [3,6): 오디오만 → nv 없음(nonverbal=null), 전사 있음
    assert by_t[4.5]["nonverbal"] is None
    assert by_t[4.5]["transcript"] == "조각" and by_t[4.5]["transcript_final"] is True
    # partial들은 turn_end보다 *먼저*, t-오름차순으로(소비자 정렬 없이도 파일이 그 순서).
    types = [r["type"] for r in parsed]
    assert types == ["window", "window", "turn_end"]
    assert [r["t"] for r in windows] == [1.5, 4.5]              # sweep이 t-정렬해 flush
    assert parsed[-1]["transcript_full"] == "조각"              # 전사 있던 윈도우만 합본에 기여
