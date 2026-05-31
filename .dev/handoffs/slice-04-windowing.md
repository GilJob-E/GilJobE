# Handoff — Slice 4: windowing (TurnWindower) (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §4`(emit) 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

gje `SlidingWindowPipeline` → giljobe `pipeline/windowing.TurnWindower` 재구현 완료.
gje의 동시성 불변식(race-free 스케줄·연속prefix covered_until·예외 swallow+log·_closed 가드·
end_turn은 풀 평가 안 기다림)을 **그대로 유지**하고, giljobe용으로 **stt 레인**(nv와 같은 3s
그리드, 윈도우별 nv·전사 페어), **transcript_full 합본 + turn_end 이벤트**(compact tail eval을
turn_end.eval에 임베드), **reset()**(턴 경계)를 추가. **오프라인 37 passed**(기존 20 + windowing
17). 6-차원 적대적 리뷰 통과(확정 5건 모두 minor, 전부 반영). *GPU/브라우저 불요 슬라이스.*

## 무엇을 만들었나

```
src/giljobe/pipeline/__init__.py        (신규, 빈) pipeline subpackage 첫 생성
src/giljobe/pipeline/windowing.py       (신규) TurnWindower + InlineExecutor + _Critic Protocol
src/giljobe/models/signals.py           (수정) TranscriptSignal + TurnEnd 2개 추가
tests/test_windowing.py                 (신규, 오프라인) 17 테스트 (mock + 실 스레드풀)
```

### windowing.py 설계 (gje turn_pipeline.py 대비 델타 — 그 외는 불변식 동형)

1. 클래스 `SlidingWindowPipeline` → `TurnWindower`(giljobe 소유). `_Evaluator` Protocol →
   `_Critic`(3메서드: read_nonverbal/transcribe_window/evaluate_window). import는 cross=absolute
   (`from giljobe.models.signals import …`).
2. **stt 레인 추가**: nv와 **같은 3s 그리드**. `poll`/`end_turn`이 한 윈도우에서 nv(`if frames`)와
   stt(`if pcm`)를 **둘 다 제출**(레코드별 페어링 보장 — 별도 counter 없이 단일 그리드 공유).
   `_do_stt`는 `transcribe_window`로 전사 → `_transcripts[start]=text` 적재(합본 재료) +
   `TranscriptSignal(t,window_s,text)` emit(실시간 UX). nv와 **같은 (t=start+dur/2, window_s)**.
3. **transcript_full + turn_end**: `end_turn(now)`이 t-정렬(start 순) 전사 합본을 만들고
   `TurnEnd(t, transcript_full, eval)`를 emit+반환. **compact tail eval을 turn_end.eval에 임베드**
   (gje는 standalone emit) — 소비자가 같은 eval을 두 번 안 받게. `_do_tail`은 emit 안 하고 sig만
   **반환**(end_turn이 임베드), 실패 시 None.
4. **레인 4개**: nv/stt/eval/**tail 분리**(latency 근거: 풀 eval 7.7s가 compact tail을 막지 않게
   tail_exec 전용 — `.dev/latency_sim.json`). `executor` 하나 주입=공유(InlineExecutor 테스트용),
   넷 따로 주입=앱 공유, 다 None=자체 생성. **부분 주입은 ValueError로 거부**(리뷰 반영).
5. **reset()**(/turn/start): 버퍼·스케줄 counter·covered_until·done_ends·transcripts·_out·_closed
   초기화(executor·설정 유지). 선결조건=이전 턴 idle(서버 책임; 엄밀 격리 필요 시 턴마다 새 윈도워).
6. worker→loop는 `on_signal`로만(서버가 `loop.call_soon_threadsafe`로 큐 투입 — 이벤트루프 비차단).
   `drain()`/`wait_idle()` 유지(테스트·종료 정리).

### models/signals.py 추가 (in-process 계약 — emit이 pipeline 의존 금지 → 공유 계약은 models에)

- `TranscriptSignal(t, window_s, text)`: nv와 같은 (t,window_s)로 윈도우별 nv·전사 조인 키.
- `TurnEnd(t, transcript_full, eval: EvaluationSignal | None)`: 턴 종합. `to_dict`는 eval None-safe 중첩.
- (Slice 5 `emit/sink.py`가 이들 + NonVerbal/Eval을 `t`로 정렬·직렬화. `models/records.py`의 JSON
  스키마와는 별개 — 여기 둘은 **윈도워가 내는 in-process 도메인 이벤트**.)

## ★ 적대적 리뷰 (6-차원 워크플로: fidelity/concurrency/spec/layering/emit/tests + 발견별 검증)

7 raw → **5 확정(전부 minor), 0 불확실, 2 반박**. fidelity·concurrency-correctness·spec·emit 핵심
**확정 결함 0건**(core 동시성 건전 확인). 확정 5건 전부 반영:

1. **부분 executor 주입 → None.submit() AttributeError**(usage error) → `__init__`에 ValueError 가드.
2. **stt future 타임아웃 시 transcript_full 조용히 부분**(silent drop) → `wait` 반환 캡처해 미완 stt
   `logger.warning`(gje "silent drop 방지" 원칙). 합본은 best-effort 명시.
3. **transcript_full 합본을 InlineExecutor로만 증명**(실 스레드풀 wait 미증명) →
   `test_end_turn_waits_for_async_stt_before_assembling_transcript_full`(느린 stt 0.2s, 실 풀,
   합본 완전성=기다림 증거 + 하한 타이밍).
4. **eot_deadline 타임아웃 경로 미테스트** → `test_end_turn_stt_timeout_warns_and_is_best_effort`
   (stt 0.5s >> deadline 0.1s: 데드라인에 끊김·부분 합본·경고 로깅·compact eval은 그대로).
5. **reset() 이전턴 in-flight emit 누수**(문서화된 YAGNI; 검증자: 리뷰어의 `_closed=True` 픽스는
   오히려 턴2 emit을 깨므로 틀림) → **동작 유지, 문서만 명확화**(서버 책임 + 턴마다 새 윈도워 대안).

테스트 타이밍 단언은 **하한만**(상한 마진은 부하 플레이크 — Slice 3 교훈). 레인독립·중복없음은
gate/Barrier **구조적 증명**(벽시계 마진 아님).

## 검증 결과 (PASS)

- `pytest tests/test_windowing.py` → **17 passed**.
- 전체 오프라인 `… test_critic_json test_mocks test_window_assembly test_windowing` → **37 passed**.
- 커버: cadence(3s nv·stt / 16s eval)·1윈도우1전사 페어링·turn_end(transcript_full+compact embed,
  standalone 중복 없음)·짧은답변 whole-tail·빈전사 제외·실패윈도우 whole-tail 복구·covered_until
  연속prefix(실패구멍 안 건너뜀 / 성공 prefix 전진)·동시 poll 중복없음(Barrier 8스레드)·레인독립
  (gate)·tail레인 독립(막힌 eval과 무관)·실 스레드풀 stt wait·타임아웃 경고·부분주입 거부·reset·close.
- vLLM/GPU 불요(NEXT.md item 3 명시) → 라이브 게이트 없음, 오프라인 통과가 완료 기준.

## 다음 슬라이스 = Slice 5: emit + 전송 — PLAN §4 / §7-5

- `models/records.py`: `WindowRecord`(type=window, nv+전사 **병합**, t로 self-describing) +
  turn_end JSON 스키마(transcript_full + 선택 eval). PLAN §4 예시 스키마.
- `emit/sink.py`: `Sink` 프로토콜 + `SSESink`(aiohttp StreamResponse `GET /signals`) + `JSONLSink`
  (리플레이/감사) + (선택, config) `WebhookSink`. **추천 v1 = SSE + JSONL**(PLAN §4 트레이드오프표).
- **조인 책임**: 윈도워는 in-process 이벤트(NonVerbal/Transcript/Eval/TurnEnd)를 **완료순**으로 낸다.
  Sink가 `t`(nv·transcript는 같은 t)로 정렬·페어링해 `WindowRecord`로 직렬화. transcript_full은
  이미 turn_end에 합본돼 있음. → Slice 4 산출물 `TranscriptSignal`/`TurnEnd`를 그대로 소비.
- 검증: `test_emit_schema`(WindowRecord 형태·nv+전사 병합·turn_end t-정렬·JSONL 유효성). *GPU 불요.*
- import: `emit`은 **models만** 의존(pipeline 의존 금지 — 그래서 Slice 4가 계약을 models에 뒀다).
  cross=absolute.

## git

이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안: `feat(slice4): TurnWindower windowing + stt lane + turn_end; adversarial review pass`.
- Slice 3 latency 원자료(`.dev/latency_sim.json` 등 NEXT.md 인용)는 미추적이었어 별도 chore로 추적.
