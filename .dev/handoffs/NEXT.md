# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 5 완료 시점)
- **완료: Slice 1** — 스캐폴드 + gje 4모듈 verbatim 복사 + 계층형 src. 상세 → `.dev/handoffs/slice-01-scaffold.md`.
- **완료: Slice 2** — ★ STT 필요성 스파이크 **PASS**. 상세 → `.dev/handoffs/slice-02-stt-spike.md`.
  - **결정: `GemmaNativeTranscriber` 기본 STT, faster-whisper 안 붙임**(VRAM 추가 0). 정본 evidence: `tests/evidence/spike-transcribe.json`.
  - ★ 버그: **한국어 user 지시문은 전사에 echo됨 → user-turn 텍스트는 영어 `"Transcribe."`** (transcriber.py 주석).
  - ★ 데이터 주의: gje 클립(vid_0001/0033)은 **영어**라 부적합. 라이브는 **`kor.mp4`**(repo 루트, 한국어, gitignore라 미커밋). 없으면 라이브 테스트 skip.
- **완료: Slice 3** — critic 포팅 + mocks. 상세 → `.dev/handoffs/slice-03-critic.md`.
  - 신규: `analysis/critic.py`(`WindowCritic` = gje `WindowEvaluator` 포팅 + 신규 `transcribe_window` 위임), `analysis/mocks.py`(`MockCritic`/`MockTranscriber`/`SlowMockCritic`), `test_critic_json.py`·`test_mocks.py`(오프라인)·`test_critic_live.py`(실 vLLM).
  - 핵심 결정: critic은 STT를 박지 않고 주입 `Transcriber`에 위임(같은 client 공유, VRAM 0). 더블은 **상태 없는 순수**(동시 레인 카운터 플레이크 회피).
- **완료: Slice 4** — windowing(`TurnWindower`). 상세 → `.dev/handoffs/slice-04-windowing.md`.
  - 신규: `pipeline/windowing.py`(`TurnWindower` = gje `SlidingWindowPipeline` 불변식 유지 재구현 + **stt 레인**(nv와 같은 3s 그리드, 윈도우별 페어) + **transcript_full 합본 + turn_end** + **reset()** + 4레인 executor), `pipeline/__init__.py`, `tests/test_windowing.py`(17, 오프라인).
  - `models/signals.py`에 **`TranscriptSignal`/`TurnEnd`** 추가(윈도워가 내는 in-process 이벤트).
  - ★ Slice 5에서 **트레일링 윈도우 nv 미대기 버그**가 발견돼 windowing.py도 수정됨(`end_turn`이 `_nv_futures`도 기다림) — 상세는 slice-05 핸드오프.
- **완료: Slice 5** — emit + 전송(records/sink). 상세 → `.dev/handoffs/slice-05-emit.md`.
  - 신규: `models/records.py`(`WindowRecord`/`EvalRecord`/`TurnEndRecord` — JSON 스키마 3종), `emit/__init__.py`, `emit/sink.py`(`Sink` 프로토콜 + `JSONLSink` + `SSESink` + `sse_frame` + **`SignalRecorder`** 조인+fan-out), `tests/test_emit_schema.py`(18, 오프라인).
  - ★ 결정: 발화중 풀 평가(채널②)도 **`eval` 레코드로 송출(A안)** — silent drop 회피. 스키마 3종(window/eval/turn_end), 전부 `t`로 self-describing(소비자가 t 정렬).
  - **조인**: nv·전사를 같은 t로 페어링→1 window 레코드 병합(`SignalRecorder`). 한 레인만 온 윈도우는 turn_end에서 t-정렬 sweep. emit은 **models만** 의존(pipeline 0).
  - SSE의 aiohttp glue는 **server 슬라이스로 의도적 연기**(이 슬라이스는 프레임 포맷+fan-out까지). WebhookSink도 실 URL 생길 때.
  - ★ 6-차원 적대적 리뷰: schema·layering 깨끗, concurrency 반박. **진짜 버그 1건(트레일링 nv silent drop) 근본 수정**(windowing.py `end_turn` nv-wait) + 회귀 가드(빼면 실패 직접 확인). 테스트 갭 5건 보강.
  - **오프라인 55 passed**(기존 37 + emit 14 + 회귀 4, windowing 17 무회귀). *GPU/브라우저 불요.*

## 다음 할 일 = Slice 6: e2e mock — PLAN §7-6
1. 먼저 `PLAN.md §6`(검증) + `.dev/handoffs/slice-05-emit.md`(다음 슬라이스 섹션) 정독.
2. **`tests/test_e2e_mock.py`**: 합성 JPEG/PCM → **실 `TurnWindower`(실 스레드풀) + `MockCritic`/`MockTranscriber` + `SignalRecorder` + `JSONLSink`** → JSONL 레코드 수·형태·turn_end 검증. aiortc·모델 제외 **전체 배선 증명**.
3. 부품은 이미 다 있음 — 새 코드 거의 없는 **테스트 위주** 슬라이스. 더블은 `analysis/mocks.py`(MockCritic/MockTranscriber/SlowMockCritic) 재사용.
4. 핵심: **실 스레드풀 완료순 emit + 소비자 t-정렬 복원**을 한 흐름(가능하면 멀티-턴/긴 턴)으로 증명. Slice 5가 이미 부분 배선 검증함(`test_windower_to_recorder_to_jsonl_end_to_end`, `..._trailing_window_nv_paired...`) → 이를 더 현실적 시나리오로 묶기.
5. 검증: `test_e2e_mock` 통과. *GPU/브라우저 불요.* (그 다음 Slice 7=ingest부터 aiortc/av/numpy/Pillow deps 추가.)
6. 리스크: **완료순 emit**(레인 지연차 → 소비자가 `t` 정렬, `turn_end.transcript_full` 보정). 단일 세션 가정(단일 `/signals` 토픽).

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
