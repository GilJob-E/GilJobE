# Handoff — Slice 6: e2e mock (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §7-7`(ingest) 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

합성 JPEG/PCM → **실 TurnWindower(실 4레인 스레드풀) + MockCritic/MockTranscriber + SignalRecorder +
JSONLSink(+SSESink)** → 소비자용 JSONL 레코드, 의 **전체 배선을 e2e로 증명**(aiortc·모델만 제외).
새 소스 코드 0줄 — 테스트(+로컬 더블)만 추가한 슬라이스. **오프라인 61 passed**(기존 55 + e2e 6),
플레이크 0/50. 5-차원 적대적 리뷰(26 에이전트)에서 확정 2건(major)을 근본 수정. *GPU/브라우저 불요.*

## 무엇을 만들었나

```
tests/test_e2e_mock.py   (신규, 오프라인) 6 테스트 + 로컬 더블 3(가변지연/게이트-eval/locked-list-sink)
```

소스(`src/giljobe/`)는 **무수정** — 부품(windowing/critic/mocks/records/sink)이 Slice 4·5에서 다 만들어졌고,
이 슬라이스는 그 부품들이 실제로 함께 배선됨을 증명하는 게 목적(PLAN §7-6).

### 6 테스트 (모두 실 스레드풀이 load-bearing — InlineExecutor면 완료순이 안 보이거나 gate가 교착)

1. **단일 턴 풀 파이프라인 → JSONL**(스모크): 9s 턴 → window 3(nv+전사 병합) + turn_end. 내용 단언은
   타이밍 무관(end_turn이 nv/stt/tail 대기). turn_end는 파일 마지막.
2. **완료순 emit → 소비자 t-정렬 복원**(PLAN §6 핵심 리스크): `_VariableLatencyCritic`(이른 t일수록
   느림)으로 파일이 t-역순으로 적힘을 직접 확인(`index(7.5) < index(1.5)`) → 소비자가 t로 정렬하면
   시간순 복원. InlineExecutor면 안 드러나는 경로.
3. **풀 eval 레인 독립성 → eval 레코드 JSONL**: `_GatedFullEvalCritic`(풀 eval만 gate 블록, compact tail
   통과). 풀 eval이 막힌 동안 window 6개는 다 떨어지고 eval 레코드는 0개(구조적 증명) → gate 풀면
   eval(채널② A안, compact=False)이 JSONL까지. *실 풀 전용*: Inline이면 poll()이 gate.wait에서 교착.
4. **멀티턴 reset 격리**: 공유 윈도워/recorder를 `reset()`으로 두 턴 → 한 JSONL에 turn_end 2개. 두 턴이
   같은 t 그리드 재사용 → 파일을 turn_end 경계로 분절해 턴별 윈도우 수·합본 검증(누수 0).
5. **멀티싱크 fan-out**: SignalRecorder가 JSONL(감사로그) + SSE(라이브) 양쪽에 동일 레코드 *멀티셋*
   송출(완료순 인터리브라 싱크별 순서는 다를 수 있으나 손실/중복 0). turn_end도 SSE로 송출됨.
6. **한-레인-only 윈도우 partial flush → JSONL**: 프레임만/오디오만 있는 윈도우가 실 풀의 poll-skip →
   end_turn nv/stt-wait → recorder sweep을 거쳐 turn_end *앞에* t-정렬 partial 레코드로(트레일링-nv
   silent drop이 났던 그 배선을 e2e로). frames-only→`transcript=""`/final=False, audio-only→`nonverbal=null`.

## ★ 적대적 리뷰 (5-차원 워크플로: flakiness/wiring-proof/coverage-gap/determinism/style + 발견별 반박 검증)

26 에이전트, 21 raw → **2 fix-now**(전부 major, 근본 수정) + 10 note-only + 9 dropped(반박됨). 깨끗:
타이밍 마진(완료순 0.1s 단위차 > 지터)·결정성·resource 정리. 확정 2건:

1. **[근본 수정] Test 3가 실 스레드풀을 증명 못 함 + Slice 5 중복**(wiring-proof). 검증자가 *실증*:
   원래 test 3는 InlineExecutor로도, 4레인을 한 executor로 합쳐도(레인 독립성 무력화) 동일 통과 →
   이름("via REAL eval lane")이 과대주장이고 `test_emit_schema.py:306`과 거의 ver바팀 중복.
   → **수정**: 리뷰 권고안(a)대로 `_GatedFullEvalCritic`로 재작성 — 풀 eval을 gate로 막고 window 6개가
   먼저 떨어지되 eval은 0개임을 구조적으로 단언, gate 풀면 eval 레코드 JSONL까지. 이제 Inline이면 poll이
   교착해 통과 불가(실증: Inline에선 poll 직후 eval count=1 → `eval==0` 단언 깨짐) = 실 풀 load-bearing.
2. **[근본 수정] partial flush가 실 풀→JSONL로 미검증**(coverage-gap). 모든 테스트가 두 레인을 다 채워
   recorder의 한-레인-only sweep 경로(`sink.py:_on_turn_end`)가 e2e로 안 돌았다. Slice 5는 hand-built
   신호(직접 on_signal)로만 봄 — 실 윈도워의 poll-skip + end_turn nv/stt-wait + sweep + JSONL은 미검증.
   → **수정**: 테스트 6 추가(frames-only/audio-only 윈도우 → 실 풀 → partial 레코드 검증).

검증 견고화(같이 반영): JSONL `close()`를 `w.close()` *뒤*로(_closed 가드 → 닫힌 파일 늦은 emit no-op,
Slice 5가 안 막던 fragility note). dropped 9건: 완료순 마진(지터보다 큼)·turn_end-last 불변식(end_turn이
nv/stt 대기 보장)·멀티턴 reset(레인 idle, eval 없음)·`shutdown(wait=False)`(유한 Mock이라 누수 없음) 등
전부 반박 또는 범위 밖(Slice 4가 이미 커버: eot_deadline trip 등)으로 정확히 판정.

## 검증 결과 (PASS)

- `pytest tests/test_e2e_mock.py` → **6 passed**. 50회 반복 **0 실패**(타이밍 민감 #2/#3 포함).
- 전체 오프라인(`test_critic_json test_mocks test_window_assembly test_windowing test_emit_schema test_e2e_mock`)
  → **61 passed**(기존 55 무회귀).
- 커버: 실 스레드풀 완료순 emit → t-정렬 복원 · 풀 eval 레인 독립성+JSONL 직렬화 · 멀티턴 reset 격리 ·
  멀티싱크 fan-out 멀티셋 동일 · 한-레인-only partial flush(트레일링-nv 경로) · turn_end-last 불변식.
- 소스 무수정 → windowing/emit 17+18 테스트 그대로. vLLM/GPU 불요 → 라이브 게이트 없음, 오프라인 통과가 완료 기준.

## 다음 슬라이스 = Slice 7: ingest (media/ingest.py) — PLAN §7-7

- `media/ingest.py`: **aiortc 트랙 소비자** — video→1fps JPEG(640×480) bytes, audio→Opus48k→16k mono s16 PCM.
  타임스탬프 부착해 `TurnWindower.add_frame/add_audio`로 투입(이벤트루프 비차단).
- ★ **새 deps 추가·설치**(이 슬라이스부터): `aiortc(~=1.14)`, `av(PyAV)`, `numpy`, `Pillow` — pyproject에
  추가(현재 deps는 `requests`만; aiortc/av/aiohttp/numpy/Pillow는 ingest/server 슬라이스 몫이라 미설치).
- 검증: `tests/test_ingest_slicing.py`(합성 PyAV 프레임) — ★ **resample-list gotcha**(`AudioResampler.resample(frame)`는
  **list 반환** → 순회·concat 안 하면 오디오 손상, PLAN §리스크) 명시 assert · 1fps throttle · PCM Int16 정렬.
  *GPU/브라우저 불요*(합성 프레임). 그 다음 Slice 8=server+브라우저, 9=라이브 e2e.

## git

이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안:
`test(slice6): e2e mock — synthetic media → real-threadpool windower → recorder → JSONL full wiring; adversarial review (gated eval-lane + partial-flush e2e)`.
- 소스 무수정, 테스트만. `.dev/` 파킹된 vision 탐색 스크래치(frame_*/pyfeat_*/vision_grounding_*)는 커밋 제외(미관련).
