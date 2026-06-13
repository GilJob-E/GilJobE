"""test_sentences — 문장 트래커(외부 전사 모드, PR #13 sideband) 오프라인 검증.

RealtimeTurnTracker는 윈도워 없이 단독 동작(결합=주입 snapper뿐) — 이벤트 시나리오를
직접 흘려 경계·텍스트·타임아웃·flush 계약을 결정적으로 검증한다(GPU/네트워크 불요).
"""
from __future__ import annotations

from giljobe.pipeline.sentences import RealtimeTurnTracker, split_sentences


def test_split_sentences_punctuation_and_interpolation():
    """종결 문장부호 분할 + 글자 비례 시간 보간. 마지막 조각 끝은 정확히 t1."""
    out = split_sentences("안녕하세요. 반갑습니다!", 0.0, 10.0)
    assert [t for (t, _, _) in out] == ["안녕하세요.", "반갑습니다!"]
    (_, s0, e0), (_, s1, e1) = out
    assert s0 == 0.0 and e1 == 10.0
    assert abs(e0 - s1) < 1e-6      # 인접 문장은 빈틈 없이 이어진다
    assert 4.0 < e0 < 6.5           # 글자 수 비례(6/12자 ≈ 절반)


def test_split_sentences_no_punctuation_is_one_sentence():
    out = split_sentences("문장부호가 없는 발화", 2.0, 5.0)
    assert len(out) == 1 and out[0][1] == 2.0 and out[0][2] == 5.0


def test_completed_with_text_emits_punct_sentences():
    """transcript.completed(전문 포함) → 발화 구간 안에서 문장 분할. VAD stop이 끝 시각."""
    tr = RealtimeTurnTracker()
    assert tr.ingest("vad.speech_started", media_now=0.5) == []
    assert tr.ingest("vad.speech_stopped", media_now=6.0) == []
    sents = tr.ingest(
        "transcript.completed", media_now=6.4,  # completed는 stop보다 늦게 도착(전송 지연)
        item_id="item1", text="첫 문장입니다. 둘째 문장입니다.",
    )
    assert [s.source for s in sents] == ["punct", "punct"]
    # 분석 단위 = '직전 끝 → 이번 끝'. speech_started는 비전/nv 윈도우 시작을 당기지 않으므로
    # 첫 문장은 턴 원점(0.0)에서 시작한다 — 발화 전 묵음도 그 윈도우에 포함(무발화 제스처 포착).
    assert sents[0].start_s == 0.0
    assert sents[0].speech_start_s == 0.5   # 프로소디 바운드는 발화 온셋(묵음 제외)
    assert sents[-1].end_s == 6.0           # VAD stop이 completed 도착 시각보다 우선
    # 다음 발화는 직전 끝에서 시작
    nxt = tr.ingest("transcript.completed", media_now=9.0, item_id="item2", text="셋째.")
    assert nxt[0].start_s == sents[-1].end_s


def test_silent_gap_attaches_to_next_sentence():
    """무발화 공백(말없이 손가락만 보이는 구간)은 다음 문장 윈도우에 귀속된다 — speech_started가
    공백을 스킵하던 버그 회귀 가드(.vision_test 손가락 4→2→5 시퀀스가 묵음 구간에서 유실됨)."""
    tr = RealtimeTurnTracker()
    s1 = tr.ingest("transcript.completed", media_now=3.0, text="첫 발화.")
    assert s1[0].start_s == 0.0 and s1[0].end_s == 3.0
    # 3.0~11.0 사이 후보가 말없이 손가락만 보임(speech_started가 와도 윈도우 시작은 안 당김)
    tr.ingest("vad.speech_started", media_now=9.0)
    tr.ingest("vad.speech_stopped", media_now=11.0)
    s2 = tr.ingest("transcript.completed", media_now=11.2, text="둘째 발화.")
    assert s2[0].start_s == 3.0          # 비전/nv 윈도우: 직전 끝부터 — 3~11s 묵음 포함
    assert s2[0].speech_start_s == 9.0   # 프로소디 바운드: 발화 온셋부터(묵음 9s 제외)
    assert s2[0].end_s == 11.0


def test_delta_accumulation_feeds_completed_without_text():
    """PR13 detail 확장에서 델타만 텍스트를 싣고 completed가 비어 있어도 누적분으로 발화 확정."""
    tr = RealtimeTurnTracker()
    tr.ingest("transcript.delta", media_now=1.0, item_id="a", text="누적 ")
    tr.ingest("transcript.delta", media_now=2.0, item_id="a", text="텍스트입니다.")
    sents = tr.ingest("transcript.completed", media_now=3.0, item_id="a")
    assert len(sents) == 1 and sents[0].text == "누적 텍스트입니다."


def test_metadata_only_events_emit_empty_utterance_boundaries():
    """현 PR13 중계(텍스트 없음): completed 시간만으로 발화 경계 — 1s 미만은 스팸 방지로 버림."""
    tr = RealtimeTurnTracker()
    short = tr.ingest("transcript.completed", media_now=0.5)
    assert short == []                       # 0.5s 미만 빈 발화는 무시
    sents = tr.ingest("transcript.completed", media_now=4.0)
    assert len(sents) == 1 and sents[0].text == "" and sents[0].source == "utterance"
    assert sents[0].end_s == 4.0


def test_timeout_backstop_forces_boundary_with_pending_deltas():
    """completed가 오래 안 오면 누적 델타로 강제 경계(실시간성 백스톱) — 빈 누적이면 경계만 전진."""
    tr = RealtimeTurnTracker(timeout_s=10.0)
    assert tr.tick(9.0) == []                # 타임아웃 전
    tr.ingest("transcript.delta", media_now=5.0, item_id="a", text="끝나지 않는 발화")
    sents = tr.tick(11.0)
    assert len(sents) == 1 and sents[0].source == "timeout"
    assert sents[0].text == "끝나지 않는 발화"
    assert tr.tick(22.0) == []               # 누적 없음 → no-op
    # ★빈 tick이 경계를 전진시키면 안 된다 — 발화 시작 앵커 보존(라이브 하니스로 잡은 회귀)
    late = tr.ingest("transcript.completed", media_now=25.0, text="늦은 발화.")
    assert late[0].start_s == sents[0].end_s  # 직전 *텍스트* 경계에서 이어진다(22.0 아님)


def test_flush_drains_remainder_and_snapper_applies():
    """턴 종료 flush가 미완 델타를 마지막 문장으로 — snapper가 경계를 휴지 위치로 보정."""
    tr = RealtimeTurnTracker(snapper=lambda t: round(t) - 0.2)  # 가짜 스냅: 근처 휴지로 당김
    tr.ingest("transcript.delta", media_now=1.0, item_id="a", text="마지막 조각")
    sents = tr.flush(5.0)
    assert len(sents) == 1 and sents[0].source == "flush" and sents[0].text == "마지막 조각"
    # completed 경로의 스냅 적용 확인
    tr2 = RealtimeTurnTracker(snapper=lambda t: 3.5)
    out = tr2.ingest("transcript.completed", media_now=4.1, text="스냅 확인.")
    assert out[0].end_s == 3.5


def test_reset_clears_turn_state():
    tr = RealtimeTurnTracker()
    tr.ingest("transcript.delta", media_now=1.0, item_id="a", text="이전 턴 잔여")
    tr.ingest("transcript.completed", media_now=4.0, item_id="x", text="이전 턴 문장.")
    tr.reset()
    assert tr.flush(2.0) == []               # 델타 잔여가 새 턴으로 새지 않는다
    sents = tr.ingest("transcript.completed", media_now=3.0, text="새 턴.")
    assert sents[0].start_s == 0.0           # 경계 원점 복귀
