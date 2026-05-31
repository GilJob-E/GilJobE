# Handoff — Slice 5: emit + 전송 (records/sink) (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §7-6`(e2e mock) 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

윈도워의 완료순 in-process 신호(NonVerbal/Transcript/Eval/TurnEnd) → 소비자용 JSON 레코드
직렬화 + Sink 송출 완료. **조인 책임**(nv·전사를 같은 t로 페어링해 1 window 레코드로 병합)을
`SignalRecorder`가 갖고, 전송은 `Sink`(JSONL/SSE)가 담당. 발화중 풀 평가(채널②)는 **A안 확정 —
`eval` 레코드로 송출**(silent drop 회피). **오프라인 55 passed**(기존 37 + emit 14 + 적대적
리뷰 회귀 4). 6-차원 적대적 리뷰에서 **진짜 결함 1건(트레일링 nv silent drop)**을 잡아 근본
수정. *GPU/브라우저 불요 슬라이스(라이브 게이트 없음).*

## 무엇을 만들었나

```
src/giljobe/models/records.py     (신규) WindowRecord/EvalRecord/TurnEndRecord — JSON 스키마 3종 + 빌더
src/giljobe/emit/__init__.py      (신규, 빈) emit subpackage 첫 생성
src/giljobe/emit/sink.py          (신규) Sink 프로토콜 + JSONLSink + SSESink + sse_frame + SignalRecorder
src/giljobe/pipeline/windowing.py (수정) end_turn이 트레일링 nv future를 기다리도록 — 적대적 리뷰 근본 수정
tests/test_emit_schema.py         (신규, 오프라인) 18 테스트 (단위 + 실 윈도워 배선 + 실 스레드풀 회귀)
```

### 출력 JSON 스키마 3종 (models/records.py — PLAN §4)

- `window`: 한 3s 윈도우의 nv(`nonverbal:{state,intensity,note}`) + 전사(`transcript`,`transcript_final`)
  **병합**. `t`=윈도우 중심. 한 레인만 오면 `nonverbal=null`(비디오 없음) 또는 `transcript=""`(오디오 없음).
- `eval`: 발화중 16s 풀 평가(채널②, `compact=false`). `t`=윈도우 중심, `eval:{...full...}` 중첩. **A안**.
- `turn_end`: `transcript_full`(면접관 LLM 실제 입력) + 선택 compact `eval`. `t`=턴 종료 시각.
- 셋 다 `type`·`t`로 self-describing — 신호가 완료순(시간순 아님)으로 나오므로 **소비자가 t로 정렬**.

### emit/sink.py 설계

1. `Sink` 프로토콜(`emit(record: dict)`, 비차단). `JSONLSink`(라인 atomic append+flush, 리플레이/감사),
   `SSESink`(`data: {json}\n\n` 프레임 1:N broadcast, 끊긴 구독자 자동 제거), `sse_frame()`(순수 포맷터).
2. **`SignalRecorder`**(조인+fan-out): nv·전사 페어링 상태(`_pending: t→{nv,tx}`)만 보유. 두 레인 다
   도착하면 즉시 병합 window 레코드 emit(실시간), 한 레인만 온 윈도우는 `turn_end`에서 t-정렬 sweep해
   부분 레코드로 flush. 풀 평가→eval 즉시, TurnEnd→turn_end(잔여 partial 먼저). `reset()`(턴 경계).
3. **스레딩**: on_signal이 윈도워 워커 스레드들에서 동시 호출될 수 있어 `_pending`을 lock 보호, fan-out은
   lock 밖(윈도워 'build under lock, act outside' 관례). Sink 실패는 로깅·격리(silent drop 방지).
4. **import: emit은 `models`만 의존**(pipeline 의존 0 — grep 검증). cross=absolute. WebhookSink는
   PLAN대로 실제 다운스트림 URL 생길 때 추가(지금은 SSE + JSONL).
5. **의도적 연기**: SSE의 aiohttp StreamResponse glue는 server 슬라이스(거기서 aiohttp 추가). 이 슬라이스는
   프레임 포맷+fan-out까지만(deps-when-needed 원칙) — 둘 다 오프라인 테스트됨.

## ★ 적대적 리뷰 (6-차원 워크플로: schema/join/concurrency/layering/silentdrop/tests + 발견별 검증)

16 에이전트, 10 raw → **6 확정 in-scope**(실질 3 클러스터). 깨끗: schema-fidelity·layering(0건),
concurrency(2 raw → 전부 반박). **확정 결함 1건이 진짜 코드 버그**라 근본 수정:

1. **[근본 수정] 트레일링 윈도우 nv silent drop**(robustness=major / join=minor, 동일 결함). `end_turn`이
   트레일링 부분윈도우에 nv+stt를 둘 다 제출하지만 **stt만 기다리고 nv는 안 기다렸다**. 실측 nv 0.69s >
   stt 0.19s라 실 스레드풀에서 nv가 TurnEnd emit *후* 도착 → recorder가 그 윈도우를 `nonverbal=None`(=
   "비디오 없음"이라 거짓)인 transcript-only 레코드로 sweep하고 늦은 nv를 고아로 버림(다음 reset에서 silent
   drop). 윈도워가 헤더에 명시한 "레코드별 nv·전사 페어링 보장" 불변식을 트레일링 윈도우에서 위반.
   → **수정**: windowing.py에 `_nv_futures` 추적(`track_nv`) 추가, `end_turn`의 wait 세트에 in-flight nv
   포함. nv가 TurnEnd 전에 완료돼 recorder가 nv·전사를 한 레코드로 병합. 미완 nv도 경고(silent drop 방지).
   wait는 tail(2.0s)이 지배해 nv(0.69s) 추가는 critical-path 무영향. **17 windowing 테스트 무회귀.**
   → **회귀 가드 검증**: nv를 wait에서 빼면 새 테스트가 `assert None is not None`으로 실패함을 직접 확인.
2. **[테스트 갭] eval 레코드 실 윈도워 e2e 미검증**(major). 기존 e2e는 9s라 16s eval 레인 미발화 → eval
   레코드 경로가 hand-built 신호로만 증명됨. → 18s 턴 실 윈도워 e2e 테스트 추가(eval t=8.0/window_s=16.0/
   compact=False 단언).
3. **[minor 3건] 보강**: (a) 멀티스레드 on_signal `_lock` 계약 회귀 테스트(같은 t nv/tx 동시 도착→윈도우당
   1 병합, 손실/중복 0), (b) 혼합 턴 cross-type t-정렬 테스트(window·partial·eval·turn_end 섞여도 t로
   시간순 복원 — PLAN §4 계약), (c) `SignalRecorder.reset()` 독스트링 보강(윈도워.reset() 트레이드오프와
   대칭) + 잔여 partial 폐기 시 경고 로깅(가시성).

검증자 반박 2건(concurrency 차원): production 배선은 `loop.call_soon_threadsafe`로 단일스레드 직렬화라
경합 미도달 — `_lock`은 직접-멀티 배선(server가 채택 시) 방어 가드. 범위 밖으로 정확히 판정됨.

## 검증 결과 (PASS)

- `pytest tests/test_emit_schema.py` → **18 passed**.
- 전체 오프라인(`test_critic_json test_mocks test_window_assembly test_windowing test_emit_schema`)
  → **55 passed**(windowing 17 무회귀).
- 커버: nv+전사 병합(both/order-independent)·한 레인 partial sweep(nv-only·tx-only)·turn_end 잔여
  t-정렬 flush·eval 레코드(중심 t)·turn_end(transcript_full+compact/None)·완료순→t-정렬·reset·JSONL
  라운드트립(한글 보존)·SSE 프레임(개행 안전·끊긴 구독자 제거)·실 윈도워 배선(window+turn_end / eval e2e)·
  **실 스레드풀 트레일링 nv 페어링 회귀**·멀티스레드 on_signal·혼합 턴 cross-type 정렬.
- vLLM/GPU 불요 → 라이브 게이트 없음, 오프라인 통과가 완료 기준.

## 다음 슬라이스 = Slice 6: e2e mock — PLAN §7-6

- `tests/test_e2e_mock.py`: 합성 JPEG/PCM → **실 TurnWindower(실 스레드풀) + MockCritic/MockTranscriber +
  SignalRecorder + JSONLSink** → JSONL 레코드 수/형태·turn_end 검증. aiortc·모델 제외 **전체 배선 증명**.
- 이미 부품은 다 있다: 이 슬라이스가 윈도워→recorder→JSONL 배선을 InlineExecutor·실 스레드풀 둘 다로
  부분 검증함(`test_windower_to_recorder_to_jsonl_end_to_end`, `..._trailing_window_nv_paired...`). e2e mock은
  이를 더 현실적인 멀티-턴/긴 턴 시나리오로 묶고, **실 스레드풀 완료순 emit + t-정렬 복원**을 한 흐름으로 증명.
- 새 코드 거의 없음(테스트 위주). 필요한 더블은 mocks.py에 이미 있음(MockCritic/MockTranscriber/SlowMockCritic).
- 검증: `test_e2e_mock` 통과. *GPU/브라우저 불요.* (그 다음 Slice 7=ingest는 aiortc/av/numpy/Pillow deps 추가.)

## git

이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안: `feat(slice5): emit records+sink (SSE/JSONL) + SignalRecorder join; fix trailing-nv silent-drop in windower; adversarial review pass`.
- windowing.py 수정은 Slice 4 코드지만 **giljobe 소유 코드의 진짜 버그**(자기 불변식 위반)라 같은 커밋에 포함.
