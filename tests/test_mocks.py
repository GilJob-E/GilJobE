"""테스트 더블 오프라인 검증 — MockCritic/MockTranscriber/SlowMockCritic이 vLLM/ffmpeg
없이 critic 표면을 흉내내는지 확인(GPU/모델 불요). windowing(Slice 4)·e2e mock(Slice 6)이
이 더블 위에 배선을 검증하므로, 더블 자체의 계약을 먼저 못박는다.
"""
from __future__ import annotations

import concurrent.futures
import time

from giljobe.analysis.mocks import MockCritic, MockTranscriber, SlowMockCritic
from giljobe.models.signals import NONVERBAL_STATES, EvaluationSignal, NonVerbalSignal


def test_mock_transcriber_fixed_string_and_empty():
    tx = MockTranscriber(text="고정전사")
    assert tx.transcribe(b"some pcm") == "고정전사"
    assert tx.transcribe(b"") == ""  # 빈 PCM이면 빈 문자열(실 transcriber 계약과 동일)


def test_mock_critic_read_nonverbal_echoes_window_and_valid_state():
    mc = MockCritic()
    nv = mc.read_nonverbal([b"jpeg"], b"pcm", t=4.5, window_s=3.0)
    assert isinstance(nv, NonVerbalSignal)
    assert nv.t == 4.5 and nv.window_s == 3.0  # 윈도우 좌표 echo (어느 윈도우의 신호인지)
    assert nv.state in NONVERBAL_STATES
    assert 0.0 <= nv.intensity <= 1.0


def test_mock_critic_transcribe_delegates_to_injected_transcriber():
    mc = MockCritic(transcriber=MockTranscriber(text="주입된 전사"))
    assert mc.transcribe_window(b"pcm", t=4.5, window_s=3.0) == "주입된 전사"
    assert mc.transcribe_window(b"", t=0.0, window_s=3.0) == ""  # 빈 PCM 위임도 빈 문자열


def test_mock_critic_evaluate_full_and_compact():
    mc = MockCritic()
    full = mc.evaluate_window([b"jpeg"], b"pcm", window_start_s=0.0, window_dur_s=16.0)
    assert isinstance(full, EvaluationSignal)
    assert full.compact is False
    assert full.window_start_s == 0.0 and full.window_dur_s == 16.0
    assert full.verbal and full.vocal and full.visual  # 풀 비평은 3축 채움
    assert full.critique  # critique 강제(비어있지 않음)

    compact = mc.evaluate_window([b"jpeg"], b"pcm", window_start_s=12.0, window_dur_s=3.0, compact=True)
    assert compact.compact is True
    assert compact.window_start_s == 12.0
    assert compact.key_observations and compact.critique  # 2키 최소 스키마 모양


def test_mock_critic_warmup_is_noop():
    MockCritic().warmup()  # 예외 없이 통과하면 OK(no-op)


def test_slow_mock_default_slow_eval_fast_nv_stt():
    """기본 SlowMockCritic은 eval만 느리고 nv/stt는 즉시 — 레인 독립성 검증의 토대."""
    slow = SlowMockCritic(eval_delay=0.3)

    t0 = time.perf_counter()
    slow.read_nonverbal([b"jpeg"], b"pcm", t=0.0, window_s=3.0)
    slow.transcribe_window(b"pcm", t=0.0, window_s=3.0)
    fast_elapsed = time.perf_counter() - t0
    assert fast_elapsed < 0.2  # nv+stt는 sleep 없음

    t0 = time.perf_counter()
    slow.evaluate_window([b"jpeg"], b"pcm", window_start_s=0.0, window_dur_s=16.0)
    eval_elapsed = time.perf_counter() - t0
    assert eval_elapsed >= 0.3  # eval은 지연


def test_slow_mock_eval_does_not_block_nv_in_threadpool():
    """레인 독립성 핵심: 별도 워커(레인)에서 도는 느린 eval이 nv read를 막지 않는다.
    windowing(Slice 4)의 nv_exec/eval_exec 분리가 지키려는 불변식을 더블 수준에서 먼저 확인.

    벽시계 마진(GIL 스케줄 지터에 취약 — 부하 시 플레이크)이 아니라 **구조적**으로 증명한다:
    eval이 2s를 자는 동안 nv가 넉넉한 1s 안에 끝나고, 그 시점에 eval이 *아직 안 끝났음*을
    확인한다. nv가 eval 뒤로 직렬화됐다면 1s 타임아웃에서 result()가 터져 회귀를 잡는다.
    """
    slow = SlowMockCritic(eval_delay=2.0, nv_delay=0.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        eval_fut = ex.submit(
            slow.evaluate_window, [b"jpeg"], b"pcm", window_start_s=0.0, window_dur_s=16.0
        )
        nv_fut = ex.submit(slow.read_nonverbal, [b"jpeg"], b"pcm", t=0.0, window_s=3.0)
        nv = nv_fut.result(timeout=1.0)  # eval은 2s 자므로 nv가 직렬화됐다면 1s에서 타임아웃
        assert isinstance(nv, NonVerbalSignal)
        assert not eval_fut.done()       # 구조적 증명: nv가 끝난 시점에 eval은 아직 진행 중
        eval_fut.result(timeout=3.0)     # eval도 결국 완료
