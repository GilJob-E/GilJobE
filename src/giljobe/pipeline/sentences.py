"""문장 경계 추적 — Realtime sideband 이벤트(외부 STT)를 미디어 시계의 문장 구간으로 바꾼다.

giljob-docker PR #13(realtime 브랜치) 계약: 브라우저가 OpenAI Realtime 전사 델타·VAD 이벤트를
API sideband로 보내고, API가 `POST {analysis-engine}/realtime/turn-events`로 중계한다. 현재
중계 레코드는 메타데이터 전용(eventKind·readiness — `rawTranscriptLogged: False`)이고,
`detail{transcript,itemId}`은 내부 확장 시 실린다. 이 모듈은 양쪽 모두에서 동작한다:
  - 텍스트가 실리면: 발화 텍스트를 종결 문장부호로 분할(글자-시간 보간) → 진짜 문장 구간.
  - 메타데이터만 오면: VAD/completed 시간만으로 발화 경계(text="") — 측정 레인은 그대로 돈다.

시간 모델: 이벤트에 신뢰할 미디어 타임스탬프가 없으므로(브라우저 wall-clock뿐) **도착 시점의
미디어 시계**(media_now, 전송 지연 ~수백 ms)를 원시 경계로 쓰고, 주입된 snapper(우리가 받는
오디오의 실측 휴지 위치, analysis/prosody.silence_run_positions)로 정밀화한다 —
.dev/llm_handoff/boundary_sim.py 검증 설계(스냅 적중 71~90%) 그대로.

타임아웃 백스톱: completed 없이 timeout_s 초과 시 누적 텍스트로 강제 경계 — 고정 그리드가
"타임아웃=3s인 퇴화 케이스"가 되는 실시간성 보장(★1순위) 메커니즘.

스레드 안전: ingest/tick은 서버 루프·poll에서 불린다(내부 lock). 윈도워와의 결합은
콜백(snapper)뿐 — 윈도워 없이 단독 테스트 가능.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

SENT_END = re.compile(r"(?<=[.?!])\s+")
MIN_SENTENCE_S = 0.2     # 이보다 짧은 구간은 경계 노이즈로 보고 버린다
MIN_EMPTY_UTTER_S = 1.0  # 텍스트 없는 발화는 이 길이 이상일 때만 emit(스팸 방지)


@dataclass
class Sentence:
    """확정된 문장 구간 — 윈도워가 이 구간으로 채널을 자른다.

    분석 윈도우는 두 갈래: 비전/nv는 [start_s, end_s](직전 문장 끝→이번 끝, 묵음 포함 —
    무발화 제스처 포착), 프로소디는 [speech_start_s, end_s](발화 온셋부터 — 묵음 희석 방지).
    speech_start_s가 None이면 발화 온셋 단서가 없는 경로(timeout/flush)라 start_s로 폴백한다."""

    start_s: float
    end_s: float
    text: str
    source: str  # punct(문장부호) | utterance(발화 통째) | timeout(백스톱) | flush(턴 종료)
    speech_start_s: float | None = None


def split_sentences(text: str, t0: float, t1: float) -> list[tuple[str, float, float]]:
    """발화 텍스트를 종결 문장부호로 분할, 글자 비례로 [t0,t1] 안에 시간 보간.
    부호가 없으면 발화 전체가 1문장(Realtime 전사는 발화 단위로 오므로 자연 단위)."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    pos = 0
    for m in SENT_END.finditer(text):
        pieces.append(text[pos:m.start()])
        pos = m.end()
    pieces.append(text[pos:])
    pieces = [p.strip() for p in pieces if p.strip()]
    out: list[tuple[str, float, float]] = []
    total = sum(len(p) for p in pieces)
    cursor = t0
    for p in pieces:
        end = t1 if p is pieces[-1] else cursor + (t1 - t0) * len(p) / max(total, 1)
        out.append((p, round(cursor, 2), round(end, 2)))
        cursor = end
    return out


class RealtimeTurnTracker:
    """sideband 이벤트 스트림 → Sentence 목록. 한 턴 수명(reset로 재사용).

    이벤트 종류는 관용적으로 매칭한다(브라우저 normalizedType / API eventKind / 원시 type 혼용):
    transcript.delta(텍스트 누적) · transcript.completed(발화 확정=경계) ·
    vad.speech_started/stopped(시간 단서). 그 외는 무시(미래 이벤트에 안전)."""

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        snapper: Callable[[float], float] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self._snap = snapper or (lambda t: t)
        self._lock = threading.Lock()
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._items: dict[str, str] = {}   # itemId → 델타 누적 텍스트
        self._order: list[str] = []        # 델타 도착 순(완료가 itemId 없이 올 때의 폴백 조립 순서)
        self._seg_start = 0.0              # 분석 윈도우 시작 = 직전 문장 끝(비전/nv용, 묵음 포함)
        self._speech_start: float | None = None        # 이번 발화 온셋(VAD) — 프로소디 바운드용
        self._speech_stopped_at: float | None = None  # 마지막 VAD stop — completed의 끝 시각 단서

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    # ── 이벤트 소비 ──
    def ingest(
        self,
        kind: str,
        *,
        media_now: float,
        item_id: str | None = None,
        text: str | None = None,
    ) -> list[Sentence]:
        kind = (kind or "").lower()
        with self._lock:
            if "transcript.delta" in kind:
                self._accumulate(item_id, text)
                return []
            if "speech_started" in kind:
                # seg_start(비전/nv 윈도우 시작)는 직전 문장 끝에 그대로 둔다 — 그 사이 묵음을
                # 다음 문장 윈도우에 포함시켜 무발화 제스처를 포착(묵음 스킵이 버그였음).
                # 발화 온셋은 프로소디 바운드용으로만 따로 기록(묵음이 음성 지표를 희석하지 않게).
                self._speech_start = self._snap(media_now)
                return []
            if "speech_stopped" in kind:
                self._speech_stopped_at = media_now
                return []
            if "transcript.completed" in kind:
                return self._complete_utterance(media_now, item_id, text)
        return []

    def _accumulate(self, item_id: str | None, text: str | None) -> None:
        if not text:
            return
        key = item_id or "_no_item"
        if key not in self._items:
            self._items[key] = ""
            self._order.append(key)
        self._items[key] += text

    def _take_text(self, item_id: str | None, text: str | None) -> str:
        """발화 텍스트 결정 — completed가 전문을 실으면 그걸, 아니면 누적 델타를 쓴다."""
        if text and text.strip():
            if item_id in self._items:
                self._items.pop(item_id, None)
                self._order = [k for k in self._order if k != item_id]
            return text.strip()
        key = item_id if item_id in self._items else (self._order[0] if self._order else None)
        if key is None:
            return ""
        self._order = [k for k in self._order if k != key]
        return self._items.pop(key, "").strip()

    def _complete_utterance(
        self, media_now: float, item_id: str | None, text: str | None
    ) -> list[Sentence]:
        """발화 확정 — 끝 시각은 VAD stop(있으면) 우선, 둘 다 스냅. 텍스트는 문장 분할.

        윈도우 시작(start)=직전 문장 끝 → 비전/nv가 그 사이 묵음을 덮는다. 텍스트는 발화 온셋
        (speech)~end에서 분할하고, *첫 조각만* 윈도우 시작을 start로 당겨 묵음을 흡수한다.
        speech_start_s(프로소디 바운드)는 각 조각의 발화 시작점."""
        end_raw = self._speech_stopped_at if self._speech_stopped_at is not None else media_now
        self._speech_stopped_at = None
        end = max(self._snap(end_raw), self._seg_start + MIN_SENTENCE_S)
        start = self._seg_start
        speech = self._speech_start if self._speech_start is not None else start
        speech = min(max(speech, start), end)   # [start, end]로 클램프(스냅 오차 방어)
        self._seg_start = end
        self._speech_start = None
        utter_text = self._take_text(item_id, text)
        if not utter_text:
            if end - start < MIN_EMPTY_UTTER_S:
                return []
            return [Sentence(round(start, 2), round(end, 2), "", "utterance",
                             speech_start_s=round(speech, 2))]
        out: list[Sentence] = []
        for i, (t, s0, s1) in enumerate(split_sentences(utter_text, speech, end)):
            win_start = start if i == 0 else s0   # 첫 조각만 묵음 흡수, 나머진 발화-연속
            out.append(Sentence(round(win_start, 2), s1, t, "punct", speech_start_s=s0))
        return out

    # ── 백스톱/종료 ──
    def tick(self, media_now: float) -> list[Sentence]:
        """poll마다 호출 — completed 없이 timeout 초과 시 누적 *델타*로 강제 경계(실시간성 백스톱).

        누적 텍스트가 없으면 아무것도 하지 않는다 — 경계를 전진시키면 발화 시작 앵커
        (VAD speech_started가 당겨둔 seg_start)가 지워져, 늦게 오는 completed의 발화 구간이
        sliver로 압착된다(라이브 하니스 실측으로 잡은 버그). 침묵 스킵은 speech_started 몫."""
        with self._lock:
            if media_now - self._seg_start <= self.timeout_s:
                return []
            pending = " ".join(self._items.pop(k, "") for k in list(self._order)).strip()
            self._order = []
            if not pending:
                return []
            end = max(self._snap(media_now), self._seg_start + MIN_SENTENCE_S)
            start, self._seg_start = self._seg_start, end
            return [
                Sentence(s0, s1, t, "timeout")
                for (t, s0, s1) in split_sentences(pending, start, end)
            ]

    def flush(self, media_now: float) -> list[Sentence]:
        """턴 종료 — 미완 델타 잔여를 마지막 문장으로 내보낸다(silent drop 방지)."""
        with self._lock:
            pending = " ".join(self._items.pop(k, "") for k in list(self._order)).strip()
            self._order = []
            if not pending:
                return []
            end = max(media_now, self._seg_start + MIN_SENTENCE_S)
            start, self._seg_start = self._seg_start, end
            return [
                Sentence(s0, s1, t, "flush")
                for (t, s0, s1) in split_sentences(pending, start, end)
            ]
