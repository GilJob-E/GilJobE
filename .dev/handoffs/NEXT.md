# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 4 완료 시점)
- **완료: Slice 1** — 스캐폴드 + gje 4모듈 verbatim 복사 + 계층형 src. 상세 → `.dev/handoffs/slice-01-scaffold.md`.
- **완료: Slice 2** — ★ STT 필요성 스파이크 **PASS**. 상세 → `.dev/handoffs/slice-02-stt-spike.md`.
  - **결정: `GemmaNativeTranscriber` 기본 STT, faster-whisper 안 붙임**(VRAM 추가 0). 정본 evidence: `tests/evidence/spike-transcribe.json`.
  - ★ 버그: **한국어 user 지시문은 전사에 echo됨 → user-turn 텍스트는 영어 `"Transcribe."`** (transcriber.py 주석).
  - ★ 데이터 주의: gje 클립(vid_0001/0033)은 **영어**라 부적합. 라이브는 **`kor.mp4`**(repo 루트, 한국어, gitignore라 미커밋). 없으면 라이브 테스트 skip.
- **완료: Slice 3** — critic 포팅 + mocks. 상세 → `.dev/handoffs/slice-03-critic.md`.
  - 신규: `analysis/critic.py`(`WindowCritic` = gje `WindowEvaluator` 포팅 + 신규 `transcribe_window` 위임), `analysis/mocks.py`(`MockCritic`/`MockTranscriber`/`SlowMockCritic`), `test_critic_json.py`·`test_mocks.py`(오프라인)·`test_critic_live.py`(실 vLLM).
  - 핵심 결정: critic은 STT를 박지 않고 주입 `Transcriber`에 위임(같은 client 공유, VRAM 0). 더블은 **상태 없는 순수**(동시 레인 카운터 플레이크 회피).
- **완료: Slice 4** — windowing(`TurnWindower`). 상세 → `.dev/handoffs/slice-04-windowing.md`.
  - 신규: `pipeline/windowing.py`(`TurnWindower` = gje `SlidingWindowPipeline` 불변식 유지 재구현 + **stt 레인**(nv와 같은 3s 그리드, 윈도우별 페어) + **transcript_full 합본 + turn_end**(compact tail eval을 turn_end.eval에 **임베드**) + **reset()** + 4레인 executor), `pipeline/__init__.py`, `tests/test_windowing.py`(17, 오프라인).
  - `models/signals.py`에 **`TranscriptSignal`/`TurnEnd`** 추가(윈도워가 내는 in-process 이벤트 — emit이 pipeline 의존 안 하게 공유 계약을 models에).
  - ★ 6-차원 적대적 리뷰: fidelity·concurrency·spec·emit **확정 결함 0건**, 확정 5건(전부 minor) 반영 — 부분 executor 주입 ValueError 가드, stt 타임아웃 경고(silent drop 방지), 실 스레드풀 stt-wait 테스트, eot 타임아웃 테스트, reset 문서 명확화.
  - **오프라인 37 passed**(windowing 17). *GPU/브라우저 불요 슬라이스(라이브 게이트 없음).*

## 다음 할 일 = Slice 5: emit + 전송 — PLAN §4 / §7-5
1. 먼저 `PLAN.md §4`(출력 JSON 스키마 + 송출 트레이드오프) + `.dev/handoffs/slice-04-windowing.md`(다음 슬라이스 섹션) 정독.
2. **`models/records.py`**: `WindowRecord`(type=window, nv 신호 + 해당 윈도우 전사 **병합**, `t`로 self-describing → 소비자가 t로 정렬) + turn_end JSON 스키마(transcript_full + 선택 eval). PLAN §4 예시.
3. **`emit/sink.py`**: `Sink` 프로토콜 + **`SSESink`**(aiohttp StreamResponse `GET /signals`) + **`JSONLSink`**(리플레이/감사 로그, 테스트가 라이브 소비자 없이 assert) + (선택, config) `WebhookSink`. **추천 v1 = SSE + JSONL**(PLAN §4 트레이드오프표 — 소비자가 서버측 면접관 LLM이라 DataChannel은 토폴로지 틀림).
4. **조인 책임**: 윈도워는 in-process 이벤트(`NonVerbalSignal`/`TranscriptSignal`/`EvaluationSignal`/`TurnEnd`)를 **완료순**으로 낸다. Sink가 `t`(nv·transcript는 **같은 t**)로 페어링해 `WindowRecord`로 직렬화. transcript_full은 이미 `TurnEnd`에 합본돼 있음 → Slice 4 산출물을 그대로 소비.
5. 검증: `test_emit_schema`(WindowRecord 형태·nv+전사 병합·turn_end t-정렬·JSONL 유효성). *GPU/브라우저 불요.*
6. import: **`emit`은 `models`만 의존**(pipeline 의존 금지 — Slice 4가 계약을 `models/signals.py`에 둔 이유). cross=absolute. 빈 `emit/__init__.py` 생성.
7. 리스크: **완료순 emit**(레인 지연차로 순서 뒤섞임 → 소비자는 `t`로 정렬, `turn_end.transcript_full`이 보정). 단일 세션 가정(단일 `/signals` 토픽).

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
