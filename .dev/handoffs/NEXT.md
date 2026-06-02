# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 7 완료 시점)
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
- **완료: Slice 6** — e2e mock. 상세 → `.dev/handoffs/slice-06-e2e-mock.md`.
  - 신규: `tests/test_e2e_mock.py`(6, 오프라인) — **소스 무수정**, 합성 미디어 → **실 4레인 스레드풀 TurnWindower + MockCritic/MockTranscriber + SignalRecorder + JSONLSink(+SSESink)** 전체 배선 e2e. 모든 테스트가 실 스레드풀 load-bearing.
  - 커버: 완료순 emit→t-정렬 복원(PLAN §6 리스크, 가변지연 더블) · 풀 eval 레인 독립성+eval 레코드 JSONL(gate 더블) · 멀티턴 reset 격리 · 멀티싱크 fan-out 멀티셋 동일 · 한-레인-only partial flush(트레일링-nv 경로) · turn_end-last 불변식.
  - ★ 5-차원 적대적 리뷰(26 에이전트): fix-now 2건(major) 근본 수정 — (1) test 3가 Inline으로도 통과(real-pool 미증명)+Slice5 중복 → gated eval-lane으로 재작성(Inline이면 poll 교착=실 풀 load-bearing 실증), (2) partial-flush가 실 풀→JSONL 미검증 → 테스트 추가. dropped 9건은 반박/범위밖.
  - **오프라인 61 passed**(기존 55 무회귀), 플레이크 0/50. *GPU/브라우저 불요.*
- **완료: Slice 7** — ingest(`media/ingest.py`). 상세 → `.dev/handoffs/slice-07-ingest.md`.
  - 신규: `media/ingest.py` — aiortc **트랙 소비자**. **순수 변환**(`encode_jpeg`=reformat→PIL JPEG, `PcmResampler`=48k→16k mono, `_concat_pcm`) ↔ **얇은 async glue**(`consume_video` 1fps throttle / `consume_audio` flush 꼬리)로 분리. media=**leaf**(TurnWindower 미import, 구조적 `_Windower` Protocol). 타임스탬프=`frame.time` 첫프레임 t0 정규화. `except MediaStreamError`만(취소는 전파). 추론은 여기서 호출 안 함(executor 경유).
  - ★ **resample-list gotcha** 실측·테스트로 잠금: `AudioResampler.resample()`은 **list 반환**(작은 입력=빈 list→`[0]`=IndexError), 꼬리 샘플은 **flush(resample(None))로만** → 순회+concat+flush 안 하면 손상. PyAV(av 16.1.0) API 전부 `.venv` 실제 호출로 검증(추측 0).
  - deps 추가·설치: `aiortc~=1.14`·`av`·`numpy`·`Pillow`(pyproject). aiohttp는 Slice 8 몫.
  - 인라인 적대적 리뷰(워크플로 정체로 직접 수행): **정정 버그 0**, 실제 커버리지 갭 3건 보강(mono 입력·yuv420p 웹캠 포맷·취소 전파).
  - **오프라인 73 passed**(기존 61 무회귀 + ingest 12), 플레이크 0/30. *GPU/브라우저 불요.*

## ★★ 통합 타깃 (모든 다음 슬라이스가 인지 — 메모리 `giljob-docker-integration-target`)
이 레포는 최종적으로 **`github.com/GilJob-E/giljob-docker`의 `services/analysis-engine/`**로 통합된다.
입력 모델이 PLAN과 다름: aiortc 직접 수신이 아니라 **LiveKit room subscribe**. → ingest 순수 변환·windowing
코어는 재사용, 트랙 소비 glue만 LiveKit SDK로 교체(그 seam으로 분리해 둠). 출력 소비자=`services/ai-engine`
(Gemini, HTTP+JSON, internal). 컨테이너 규약(alpine→네이티브 시 slim 협의·requirements.txt·/healthz·
AGENTS+CLAUDE 쌍)은 메모리 참조. **Slice 8 server 설계 시 의식**(데모는 aiortc로 진행하되 통합 어댑터 염두).

## 다음 할 일 = Slice 8: server + 브라우저 — PLAN §7-8
1. 먼저 `PLAN.md §7-8`(+ §아키텍처 데이터흐름, §5 턴 경계, §6 수동 스모크) + `.dev/handoffs/slice-07-ingest.md`(다음 슬라이스 섹션 + ★ poll-clock 계약) 정독.
2. **`src/giljobe/server/app.py`**: aiohttp — `POST /offer`(SDP 협상), `GET /signals`(SSE, emit/sink), `/turn/start|end`, 정적 `web/`. **poll 타이머** 소유(★ ingest는 **미디어 상대초**로 프레임 태깅 → poll(now)를 같은 타임라인으로 몰 것). ingest `consume_video/consume_audio`를 트랙별 태스크로 기동. 단일 세션 가정(앱-레벨 공유 executor·단일 윈도워·단일 /signals).
3. **`web/index.html`**: getUserMedia → RTCPeerConnection → POST /offer, `/signals` EventSource tail(데모 가시화).
4. ★ 새 dep: **aiohttp** 추가·설치(server 몫, pyproject 주석대로). 검증: `http://localhost:PORT`(localhost=secure context) 수동 브라우저 스모크 — 협상→프레임/오디오 도착→`/signals`에 레코드 표출. 종료 후 `docker stop`+`nvidia-smi`로 VRAM 위생(라이브 critic 붙일 때).
5. 리스크: **poll-clock 정합**(위, ingest note) · **getUserMedia secure context**(localhost/HTTPS) · **이벤트루프 블로킹**(critic/STT는 윈도워 executor 경유) · **통합 시 LiveKit 입력 모델**(위 ★★). (그 다음 Slice 9=라이브 e2e.)

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
