# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 3 완료 시점)
- **완료: Slice 1** — 스캐폴드 + gje 4모듈 verbatim 복사 + 계층형 src. 상세 → `.dev/handoffs/slice-01-scaffold.md`.
- **완료: Slice 2** — ★ STT 필요성 스파이크 **PASS**. 상세 → `.dev/handoffs/slice-02-stt-spike.md`.
  - **결정: `GemmaNativeTranscriber` 기본 STT, faster-whisper 안 붙임**(VRAM 추가 0). 정본 evidence: `tests/evidence/spike-transcribe.json`.
  - ★ 버그: **한국어 user 지시문은 전사에 echo됨 → user-turn 텍스트는 영어 `"Transcribe."`** (transcriber.py 주석).
  - ★ 데이터 주의: gje 클립(vid_0001/0033)은 **영어**라 부적합. 라이브는 **`kor.mp4`**(repo 루트, 한국어, gitignore라 미커밋). 없으면 라이브 테스트 skip.
- **완료: Slice 3** — critic 포팅 + mocks. 상세 → `.dev/handoffs/slice-03-critic.md`.
  - 신규: `analysis/critic.py`(`WindowCritic` = gje `WindowEvaluator` 포팅 + 신규 `transcribe_window` 위임), `analysis/mocks.py`(`MockCritic`/`MockTranscriber`/`SlowMockCritic`), `test_critic_json.py`·`test_mocks.py`(오프라인)·`test_critic_live.py`(실 vLLM).
  - 핵심 결정: critic은 STT를 박지 않고 주입 `Transcriber`에 위임(같은 client 공유, VRAM 0). 더블은 **상태 없는 순수**(동시 레인 카운터 플레이크 회피). **Critic 프로토콜 미작성**(YAGNI — Slice 4에서 필요시).
  - ★ 적대적 리뷰(7-에이전트): fidelity·interface 0건, 확정 1건(레인독립 테스트 타이밍 플레이크 → 구조적 증명으로 수정, 4-loader 60/60).
  - **오프라인 20 passed + 라이브 PASS**(nv=engaged/0.60, 한국어 전사, compact eval). 정본 `tests/evidence/critic-live.json`. GPU 위생 OK.

## 다음 할 일 = Slice 4: windowing — PLAN §5 / §7-4
1. 먼저 `PLAN.md §5`(윈도잉/동시성) + `.dev/handoffs/slice-03-critic.md`(다음 슬라이스 섹션) 정독.
2. **`pipeline/windowing.py`**: gje `SlidingWindowPipeline` 불변식 유지 재구현(giljobe 소유).
   - `add_frame(t,jpeg)`/`add_audio(t,pcm)` lock-guarded. `poll(now)`로 nv 그리드(3s) 전진·`[start,start+3)` 슬라이스·`nv_exec` submit(submit-후-전진을 lock 안에서 → 중복 없음).
   - stt 그리드 = nv와 같은 3s(레코드별 nv·전사 페어링). eval 그리드(16s, 선택): `_covered_until`은 **연속 prefix 성공시만** 전진(실패/지연은 eot tail이 재커버).
   - 턴 경계: `/turn/start`=reset, `/turn/end(now)`=부분윈도우+최종 stt flush, compact tail을 `tail_exec`로, `wait(timeout)`, `turn_end`(transcript_full) emit, `close()`로 늦은 emit no-op. worker→loop는 **`loop.call_soon_threadsafe`만**.
3. 검증: `test_windowing`(mock + 실 스레드풀) — cadence(3s/16s)·eot tail·실패윈도우 tail 복구·**중복 없음**·**레인독립(`SlowMockCritic`)**·stt 레인 1윈도우 1전사. *GPU/브라우저 불요.*
4. 이 슬라이스 산출물 재사용: `MockCritic`(배선)·`SlowMockCritic`(레인독립)·`MockTranscriber`. import: cross=absolute(`from giljobe.analysis…`).
5. 리스크(PLAN §리스크): **이벤트루프 블로킹**(critic/STT를 ingest 코루틴서 직접 호출 금지, 반드시 executor 경유), 완료순 emit(소비자가 `t`로 정렬).
6. ★ Slice 3 실측 근거(레인 설계 정당화 — 4090 1장, kor.mp4 38s 3레인 시뮬): 핫패스(nv 0.69s / stt 0.19s)는 **3s cadence 대비 4배 여유로 안 밀림**. 풀 eval은 **7.7s 롱폴**(핫패스의 ~10배)이고 **~33% JSON 절단** → eval 레인 워커는 예외 흡수 필수, eot은 풀 eval 말고 **compact tail**. 그리고 **`tail_exec`를 `eval_exec`와 반드시 분리**(공유 시 compact tail이 진행중 7.7s eval 뒤 1.2s 큐잉됨 — 실측). 원자료 `.dev/latency_sim.json`.

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
