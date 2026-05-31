"""턴 윈도워 — 라이브 webRTC 버퍼(1fps JPEG + 16kHz PCM)를 3s/16s 윈도우로 잘라
백그라운드 레인에 제출한다. gje `SlidingWindowPipeline` 불변식을 그대로 유지한 재구현이며
(giljobe 소유), giljobe용으로 **stt 레인**(nv와 같은 3s 그리드, 윈도우별 전사 페어)과
턴 종료의 **transcript_full 합본 + turn_end 이벤트**를 추가한다.

추론은 백그라운드(executor)에서 돈다 — poll/add_*는 즉시 반환(이벤트루프 비차단). 네 레인을
분리해 서로 막지 않게 한다(실측 근거: nv 0.69s / stt 0.19s / 풀 eval 7.7s / compact tail 2.0s):
  - nv 레인(_nv_exec, 3s)        : 비언어 read(채널①) → NonVerbalSignal
  - stt 레인(_stt_exec, 3s)      : 같은 윈도우 전사   → TranscriptSignal
  - eval 레인(_eval_exec, 16s, 선택): 발화중 풀 비평 → EvaluationSignal(compact=False)
  - tail 레인(_tail_exec, 워커1) : end-of-turn compact tail 전용 → turn_end.eval
nv·stt는 같은 3s 그리드라 한 poll 루프에서 둘 다 제출(레코드별 nv·전사 페어링 보장). counter는
*제출 시점*에 전진하므로 동시 poll에도 같은 윈도우를 두 번 추론하지 않는다(race-free, lock 보호).
워커 예외는 삼키지 않고 로깅한다(silent drop 방지). worker→loop 복귀는 on_signal로만(서버가
`loop.call_soon_threadsafe`로 큐 투입 — 이벤트루프 비차단).

**집계 없음(per-window)** — 신호는 완료 순으로 emit돼 시간순이 아닐 수 있다(소비자는 t로 정렬).
예외는 턴 종료의 transcript_full(t-정렬 합본)과 compact eval로, 이는 윈도워가 turn_end에서 1회
종합한다. end_turn은 답변 *마지막 구간*만 **compact 평가**(짧은 출력→지연 묶기)해 turn_end.eval에
임베드하고(standalone emit 안 함 → 소비자가 같은 eval을 두 번 받지 않음), 발화중 완성된 풀
윈도우는 이미 emit돼 있어 기다리지 않는다. 아직 도는 풀 평가는 의도적으로 버린다(그 구간은
compact tail이 재커버) — close() 후의 emit은 _closed 가드로 no-op이라 안전.

라이브: /turn/start=reset() → add_frame/add_audio로 도착분 적재 → poll(now)로 due 윈도우 백그라운드
제출 → /turn/end(now)로 트레일링 부분윈도우+최종 stt flush+compact tail, transcript_full 합본 emit.
테스트는 executor=InlineExecutor()를 주입해 동기·결정적으로 검증한다(동시성·레인독립은 실 스레드풀).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Protocol

from giljobe.models.signals import (
    EvaluationSignal,
    NonVerbalSignal,
    TranscriptSignal,
    TurnEnd,
)

logger = logging.getLogger(__name__)

# 윈도워가 on_signal/drain으로 내보내는 신호 유니온(소비자=emit/Sink가 t로 정렬·직렬화).
Signal = NonVerbalSignal | TranscriptSignal | EvaluationSignal | TurnEnd


class _Critic(Protocol):
    """윈도워가 의존하는 critic 표면(WindowCritic 또는 MockCritic이 만족)."""

    def read_nonverbal(
        self, jpeg_frames: list[bytes], pcm: bytes, *, t: float, window_s: float,
        src_fps: float = ..., sample_rate: int = ...,
    ) -> NonVerbalSignal: ...

    def transcribe_window(
        self, pcm: bytes, *, t: float, window_s: float, sample_rate: int = ...,
    ) -> str: ...

    def evaluate_window(
        self, jpeg_frames: list[bytes], pcm: bytes, *, window_start_s: float, window_dur_s: float,
        src_fps: float = ..., sample_rate: int = ..., compact: bool = ...,
    ) -> EvaluationSignal: ...


class InlineExecutor:
    """submit이 함수를 즉시 동기 실행 — 테스트/결정성용 (futures는 완료 상태로 반환).

    프로덕션 기본은 스레드풀(백그라운드)이지만, 테스트는 이걸 주입해 추론이 호출 즉시 끝나게 해
    윈도우 스케줄·합본·compact tail 로직을 GPU/타이밍 없이 결정적으로 검증한다.
    (실제 동시성·레인 독립성은 별도의 실-스레드풀 테스트로 검증.)
    """

    def submit(self, fn: Callable[..., object], *args, **kwargs) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 워커 예외를 future로 전달(스레드풀과 동형)
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:  # noqa: A002, FBT001, FBT002
        pass


class TurnWindower:
    def __init__(
        self,
        critic: _Critic,
        *,
        on_signal: Callable[[Signal], None] | None = None,
        nonverbal_window_s: float = 3.0,
        eval_window_s: float = 16.0,
        src_fps: float = 1.0,
        sample_rate: int = 16000,
        executor=None,
        nv_executor=None,
        stt_executor=None,
        eval_executor=None,
        tail_executor=None,
        eot_deadline_s: float = 3.0,
    ) -> None:
        self.ev = critic
        self.on_signal = on_signal
        self.nv_w = nonverbal_window_s
        self.eval_w = eval_window_s
        self.src_fps = src_fps
        self.sample_rate = sample_rate
        self.eot_deadline_s = eot_deadline_s  # compact tail/최종 stt를 기다리는 안전 상한(backstop)

        # 실행 레인. executor 주입 시 네 레인 공용(InlineExecutor=동기, 테스트용).
        # nv/stt/eval/tail executor를 따로 주입하면 그걸 쓰고(앱-레벨 공유), 다 None이면 자체 생성.
        if executor is not None:
            nv_executor = stt_executor = eval_executor = tail_executor = executor
        self._owns_exec = nv_executor is None
        # 부분 주입 금지 — 일부 레인만 주면 나머지 None executor에 .submit() 시 AttributeError로 죽는다.
        # 허용 패턴은 셋뿐: executor 하나로 공유 / 네 레인 모두 주입 / 전부 생략(자체 생성).
        if not self._owns_exec and None in (nv_executor, stt_executor, eval_executor, tail_executor):
            raise ValueError(
                "레인 executor는 넷(nv/stt/eval/tail)을 모두 주입하거나, executor 하나로 공유하거나, "
                "전부 생략(자체 생성)해야 한다 — 부분 주입 불가."
            )
        if self._owns_exec:
            self._nv_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gje-nv")
            self._stt_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gje-stt")
            self._eval_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gje-eval")
            self._tail_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gje-tail")
        else:
            self._nv_exec, self._stt_exec = nv_executor, stt_executor
            self._eval_exec, self._tail_exec = eval_executor, tail_executor

        self._lock = threading.Lock()       # 입력 버퍼 + 스케줄 counter + covered_until 보호
        self._out_lock = threading.Lock()   # 출력 버퍼 + _closed 보호
        self._fut_lock = threading.Lock()   # 추적 future 리스트 보호
        self._tx_lock = threading.Lock()    # 전사 합본 dict 보호(stt 워커가 짧게 잡음)
        self._frames: list[tuple[float, bytes]] = []
        self._audio: list[tuple[float, bytes]] = []
        self._next_nv_end = nonverbal_window_s
        self._next_eval_end = eval_window_s
        self._eval_covered_until = 0.0      # 풀 평가가 *연속 성공* 완료된 끝 시각 → eot compact tail 시작점
        self._eval_done_ends: set[float] = set()  # 성공한 풀 윈도우 end(그리드) — 연속 prefix 계산용
        self._transcripts: dict[float, str] = {}  # window_start → 전사 텍스트(turn_end 합본 재료)
        self._out: list[Signal] = []
        self._futures: list[Future] = []
        self._stt_futures: list[Future] = []  # end_turn이 transcript_full 합본 전 기다릴 대상
        self._closed = False

    # ── 입력 ──
    def add_frame(self, t_s: float, jpeg: bytes) -> None:
        with self._lock:
            self._frames.append((t_s, jpeg))

    def add_audio(self, t_s: float, pcm: bytes) -> None:
        with self._lock:
            self._audio.append((t_s, pcm))

    def _slice(self, start_s: float, end_s: float) -> tuple[list[bytes], bytes]:
        # 호출자가 self._lock을 잡은 상태에서 호출(버퍼 일관성 — append와 경합 방지).
        frames = [j for (t, j) in self._frames if start_s <= t < end_s]
        pcm = b"".join(p for (t, p) in self._audio if start_s <= t < end_s)
        return frames, pcm

    # ── 출력 ──
    def _emit(self, sig: Signal) -> None:
        with self._out_lock:
            if self._closed:  # close() 후 도착한 in-flight 신호는 버린다(orphaned append 방지)
                return
            self._out.append(sig)
        if self.on_signal is not None:
            self.on_signal(sig)

    def drain(self) -> list[Signal]:
        """지금까지 완료돼 모인 신호를 꺼내고 버퍼를 비운다(스레드 안전, point-in-time 스냅샷)."""
        with self._out_lock:
            out = self._out
            self._out = []
        return out

    # ── 추적/대기 ──
    def _submit(self, executor, fn: Callable[..., object], *args, track_stt: bool = False) -> Future:
        fut = executor.submit(fn, *args)
        with self._fut_lock:
            self._futures = [f for f in self._futures if not f.done()]
            self._futures.append(fut)
            if track_stt:  # stt만 별도 추적 — end_turn이 transcript_full 합본 전 이것만 기다림
                self._stt_futures = [f for f in self._stt_futures if not f.done()]
                self._stt_futures.append(fut)
        return fut

    def wait_idle(self, timeout: float | None = None) -> None:
        """제출된 모든 백그라운드 추론이 끝날 때까지 대기(테스트/종료 정리용)."""
        with self._fut_lock:
            pending = [f for f in self._futures if not f.done()]
        if pending:
            wait(pending, timeout=timeout)

    # ── 워커 잡 (예외는 로깅하고 삼킨다 — 한 윈도우 실패가 파이프라인을 죽이지 않게) ──
    def _do_nv(self, start_s: float, dur_s: float, frames: list[bytes], pcm: bytes):
        try:
            sig = self.ev.read_nonverbal(
                frames, pcm, t=start_s + dur_s / 2, window_s=dur_s,
                src_fps=self.src_fps, sample_rate=self.sample_rate,
            )
        except Exception:  # noqa: BLE001 - 워커 실패는 로깅하고 신호만 누락(다음 read가 커버)
            logger.exception("nonverbal read failed [%.1f, +%.1fs]", start_s, dur_s)
            return None
        self._emit(sig)
        return sig

    def _do_stt(self, start_s: float, dur_s: float, pcm: bytes):
        """채널① 페어 — 같은 윈도우 전사. 텍스트를 합본 dict에 적재(turn_end용)하고
        TranscriptSignal로 emit(실시간 UX). 빈 전사도 1윈도우=1전사로 내되 합본에선 제외한다."""
        t = start_s + dur_s / 2
        try:
            text = self.ev.transcribe_window(
                pcm, t=t, window_s=dur_s, sample_rate=self.sample_rate,
            )
        except Exception:  # noqa: BLE001 - 전사 실패는 로깅, 이 윈도우 전사만 누락
            logger.exception("transcribe failed [%.1f, +%.1fs]", start_s, dur_s)
            return None
        with self._tx_lock:
            self._transcripts[start_s] = text
        self._emit(TranscriptSignal(t=t, window_s=dur_s, text=text))
        return text

    def _do_eval(self, start_s: float, dur_s: float, frames: list[bytes], pcm: bytes):
        """발화중 풀 평가(채널②). 성공 시 *연속 성공* prefix만큼 covered_until 전진 후 emit."""
        try:
            sig = self.ev.evaluate_window(
                frames, pcm, window_start_s=start_s, window_dur_s=dur_s,
                src_fps=self.src_fps, sample_rate=self.sample_rate, compact=False,
            )
        except Exception:  # noqa: BLE001 - 실패 시 covered_until 전진 안 함 → eot compact tail이 재커버
            logger.exception("eval failed [%.1f, +%.1fs]", start_s, dur_s)
            return None
        # max()가 아니라 연속 prefix라야: 앞 윈도우가 실패/지연되면 covered_until이 그 구멍을
        # 건너뛰지 않고 멈춘다 → 그 미평가 구간을 end_turn compact tail이 다시 커버(누락 방지).
        with self._lock:
            self._eval_done_ends.add(start_s + dur_s)
            nxt = self._eval_covered_until + self.eval_w
            while nxt in self._eval_done_ends:
                self._eval_covered_until = nxt
                nxt += self.eval_w
        self._emit(sig)
        return sig

    def _do_tail(self, start_s: float, dur_s: float, frames: list[bytes], pcm: bytes):
        """end-of-turn compact tail. _do_eval과 달리 standalone emit하지 않고 sig를 *반환*만 한다
        — end_turn이 turn_end.eval에 임베드(소비자가 같은 eval을 두 번 받지 않게). 실패면 None."""
        try:
            return self.ev.evaluate_window(
                frames, pcm, window_start_s=start_s, window_dur_s=dur_s,
                src_fps=self.src_fps, sample_rate=self.sample_rate, compact=True,
            )
        except Exception:  # noqa: BLE001 - tail 실패는 로깅, turn_end.eval=None(전사 합본은 그대로 emit)
            logger.exception("compact tail eval failed [%.1f, +%.1fs]", start_s, dur_s)
            return None

    # ── 스케줄 ──
    def poll(self, now_s: float) -> None:
        """now_s까지 완성된 윈도우들을 백그라운드 추론에 제출(즉시 반환, race-free)."""
        nv_jobs: list[tuple[float, float, list[bytes], bytes]] = []
        eval_jobs: list[tuple[float, float, list[bytes], bytes]] = []
        with self._lock:
            while self._next_nv_end <= now_s:
                start = self._next_nv_end - self.nv_w
                self._next_nv_end += self.nv_w  # 제출 *전에* 전진 → 동시 poll 중복 방지
                frames, pcm = self._slice(start, start + self.nv_w)
                nv_jobs.append((start, self.nv_w, frames, pcm))
            while self._next_eval_end <= now_s:
                start = self._next_eval_end - self.eval_w
                self._next_eval_end += self.eval_w
                frames, pcm = self._slice(start, start + self.eval_w)
                eval_jobs.append((start, self.eval_w, frames, pcm))
        # nv·stt는 같은 윈도우 집합 — 한 윈도우에서 둘 다 제출(레코드별 nv·전사 페어링).
        for (start, dur, frames, pcm) in nv_jobs:
            if frames:  # 비디오 없는 구간은 비언어 read 스킵
                self._submit(self._nv_exec, self._do_nv, start, dur, frames, pcm)
            if pcm:  # 오디오 없는 구간은 전사 스킵
                self._submit(self._stt_exec, self._do_stt, start, dur, pcm, track_stt=True)
        for (start, dur, frames, pcm) in eval_jobs:
            if frames and pcm:  # 평가는 영상+오디오 둘 다 필요
                self._submit(self._eval_exec, self._do_eval, start, dur, frames, pcm)

    def end_turn(self, now_s: float) -> TurnEnd:
        """발화 종료: 답변 *마지막 구간*만 **compact 평가**(짧은 출력)해 지연을 묶고, 윈도우
        전사들을 t-정렬해 transcript_full로 합본한 turn_end를 낸다.

        - 풀 평가 윈도우는 새로 제출하지 않는다(7s씩 걸려 turn 종료를 막음).
        - nv·stt: due 윈도우 + 트레일링 부분윈도우(>0.5s)를 같은 집합으로 제출(페어링 유지).
        - compact tail: 완료된 커버리지 끝(`_eval_covered_until`)~now를 전용 tail 레인에서 1회 평가
          → 발화중 못 끝낸/안 돈/실패한 마지막 구간이 즉시 반영. 짧은 답변(풀 평가 0개)은 전체를 1회.
        - transcript_full을 위해 stt(+최종 stt flush)와 compact tail을 기다린다(eot_deadline backstop).
          발화중 풀 평가는 기다리지 않는다(의도적으로 버림 — compact tail이 재커버).
        - 반환한 TurnEnd는 on_signal로도 emit된다(서버가 통일된 Sink 경로로 송출)."""
        nv_jobs: list[tuple[float, float, list[bytes], bytes]] = []
        tail_jobs: list[tuple[float, float, list[bytes], bytes]] = []
        with self._lock:
            # nv·stt: due 윈도우 (저지연이라 그대로)
            while self._next_nv_end <= now_s:
                start = self._next_nv_end - self.nv_w
                self._next_nv_end += self.nv_w
                frames, pcm = self._slice(start, start + self.nv_w)
                nv_jobs.append((start, self.nv_w, frames, pcm))
            # nv·stt 트레일링 부분윈도우(최종 flush) — 0.5s 미만 자투리는 무시
            nv_tail_start = self._next_nv_end - self.nv_w
            if now_s - nv_tail_start > 0.5:
                frames, pcm = self._slice(nv_tail_start, now_s)
                nv_jobs.append((nv_tail_start, now_s - nv_tail_start, frames, pcm))
            # 평가 compact tail: 완료된 커버리지 끝~now.
            tail_start = self._eval_covered_until
            tail_dur = now_s - tail_start
            # 짧은 답변(풀 평가 0개)이면 전체가 tail이라 무조건 평가; 그 외엔 1s 미만 자투리만 무시
            # (직전 풀 윈도우가 이미 커버한 끝자락 sliver는 스킵).
            whole_answer = self._eval_covered_until == 0.0
            if tail_dur > 0.0 and (whole_answer or tail_dur > 1.0):
                frames, pcm = self._slice(tail_start, now_s)
                tail_jobs.append((tail_start, tail_dur, frames, pcm))
        # 제출 (lock 밖). compact tail → 전용 tail 레인(절대 경합 없음).
        tail_fut: Future | None = None
        for (start, dur, frames, pcm) in tail_jobs:
            if frames and pcm:  # 평가는 영상+오디오 둘 다 필요
                tail_fut = self._submit(self._tail_exec, self._do_tail, start, dur, frames, pcm)
        for (start, dur, frames, pcm) in nv_jobs:
            if frames:
                self._submit(self._nv_exec, self._do_nv, start, dur, frames, pcm)
            if pcm:
                self._submit(self._stt_exec, self._do_stt, start, dur, pcm, track_stt=True)
        # transcript_full(전사 합본)·compact eval을 종합하려면 stt와 tail을 기다린다.
        # 발화중 풀 평가는 _stt_futures/tail_fut에 없으므로 자동으로 안 기다림(의도).
        with self._fut_lock:
            pending = [f for f in self._stt_futures if not f.done()]
        if tail_fut is not None:
            pending.append(tail_fut)
        if pending:
            _, not_done = wait(pending, timeout=self.eot_deadline_s)
            # stt가 데드라인 내 못 끝나면 transcript_full이 부분이 된다 — 조용히 버리지 말고 경고
            # (gje "silent drop 방지" 원칙). 실측 stt 0.19s vs 기본 3.0s라 정상에선 안 걸린다.
            stalled_stt = [f for f in not_done if f is not tail_fut]
            if stalled_stt:
                logger.warning(
                    "end_turn: stt %d개가 eot_deadline(%.1fs) 내 미완 — transcript_full 부분일 수 있음",
                    len(stalled_stt), self.eot_deadline_s,
                )
        # transcript_full 합본: 윈도우 전사를 t-정렬(start 순)로 join, 빈 조각 제외(미완 stt는 누락 = best-effort).
        # 3s 그리드는 어절 중간 절단 가능(공백 join) — 면접관 LLM 입력 보정용, 완벽 정렬 아님.
        with self._tx_lock:
            transcript_full = " ".join(
                txt for _, txt in sorted(self._transcripts.items()) if txt.strip()
            )
        eval_sig: EvaluationSignal | None = None
        if tail_fut is not None and tail_fut.done() and tail_fut.exception() is None:
            eval_sig = tail_fut.result()  # _do_tail 실패 시 result는 None
        turn_end = TurnEnd(t=now_s, transcript_full=transcript_full, eval=eval_sig)
        self._emit(turn_end)
        return turn_end

    def reset(self) -> None:
        """/turn/start — 다음 턴을 위해 버퍼·스케줄·합본을 초기화(설정·executor는 유지).

        ★ 선결 조건(호출자=서버 책임): 이전 턴 레인이 idle일 때(end_turn 후, 턴 간 사람 공백) 호출.
        그 공백에 핫패스(nv 0.7s/stt 0.2s)는 이미 끝나 있다. 발화중 버려진 풀 평가(7.7s)가 reset
        직후 늦게 끝나면 새 턴 _out에 stale 신호가 섞일 수 있으나(소비자는 t로 자기기술), v1 단일
        세션에선 범위 밖(YAGNI). 엄밀한 격리가 필요하면 서버가 턴마다 새 TurnWindower를 만들면 된다."""
        with self._lock:
            self._frames.clear()
            self._audio.clear()
            self._next_nv_end = self.nv_w
            self._next_eval_end = self.eval_w
            self._eval_covered_until = 0.0
            self._eval_done_ends = set()
        with self._tx_lock:
            self._transcripts = {}
        with self._fut_lock:
            self._futures = []
            self._stt_futures = []
        with self._out_lock:
            self._out = []
            self._closed = False

    def close(self) -> None:
        """신호 수용 중단(_closed) 후 소유한 실행기 종료. 주입 실행기는 호출자 책임.
        end_turn 후 호출되면 아직 도는 풀 평가의 뒤늦은 emit은 _closed 가드로 no-op."""
        with self._out_lock:
            self._closed = True
        if self._owns_exec:
            self._nv_exec.shutdown(wait=False)
            self._stt_exec.shutdown(wait=False)
            self._eval_exec.shutdown(wait=False)
            self._tail_exec.shutdown(wait=False)
