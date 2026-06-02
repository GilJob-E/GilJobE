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
  - ★ **레이턴시 evidence**(`.venv` 실측): encode_jpeg 1280×720≈**1.65ms**(1회/초)·1080p 2.1ms, resample 20ms조각≈**0.013ms**(~50회/초) → ingest가 이벤트루프 잡는 시간 ≈**2.4ms/초(0.24%)** = 무시 가능. "변환은 ms급, 추론만 executor로" 설계 가정 숫자로 확인. **종단(영상→emit) 레이턴시는 실 모델 필요 → Slice 9 라이브 e2e에서 측정**(미측정 의도적).

## ★★ 통합 타깃 (모든 다음 슬라이스가 인지 — 메모리 `giljob-docker-integration-target`)
이 레포는 최종적으로 **`github.com/GilJob-E/giljob-docker`의 `services/analysis-engine/`**로 통합된다.
입력 모델이 PLAN과 다름: aiortc 직접 수신이 아니라 **LiveKit room subscribe**. **그쪽 프론트(`apps/web`)는
이미 LiveKit으로 publish 완성**(룸 `giljob-session-{id}`, 토큰 `POST /api/sessions`)이고, **브라우저 STT를
의도적으로 제거해 전사를 우리한테 위임**(최신 커밋 "Route transcription through GilJobE analysis engine").
→ 우리는 같은 룸에 **subscriber로 join → candidate track 구독 → 파이프라인 → transcript+signal emit**.
출력 소비자=`services/ai-engine`(Gemini, HTTP+JSON, internal). 컨테이너 규약(alpine→네이티브 시 slim
협의·requirements.txt·/healthz·AGENTS+CLAUDE 쌍)은 메모리 참조.

## 다음 할 일 = Slice 8: **LiveKit 구독자 입력 어댑터** (PLAN §7-8 **피벗** — 2026-06-02 사용자 결정)
> ★ **피벗 사유**: 통합 타깃 프론트가 LiveKit publish 완성 + 브라우저 STT 우리한테 위임 확인 →
> 실제 통합은 LiveKit 구독. PLAN §7-8의 **aiortc `POST /offer` + 자체 `web/index.html`은 타깃에서
> 안 쓰여 throwaway** → 그 데모 대신 **진짜(LiveKit 구독자)**를 만든다. (aiortc 기반 `consume_video/
> consume_audio`(Slice 7)는 유효한 채 남겨두되 핫패스는 LiveKit 버전으로.) **PLAN.md §7-8/§아키텍처는
> 이 피벗으로 일부 superseded** — 본 NEXT.md가 우선(정본 갱신은 LiveKit 연구 결과 반영 후).

1. 먼저 **`.dev/livekit-sdk-research.md`**(★ 실API 검증·의사코드·체크리스트 — Slice 8 시작점) + 이 섹션 +
   `.dev/handoffs/slice-07-ingest.md`(★ poll-clock 계약) + 메모리 `giljob-docker-integration-target` 정독.
2. **LiveKit 구독자**: `rtc.Room()` → `connect(url, token, RoomOptions(auto_subscribe=True))`로 `giljob-session-{id}`
   join(**hidden 구독자** — token grant `can_subscribe=True/can_publish=False/hidden=True`). candidate audio+video
   track 구독(★ 늦게 join 시 `track_subscribed` 안 오니 `room.remote_participants` 순회로 기존 track도 시작).
   토큰: services/api 발급(권장) vs 우리 직접 발급(`livekit-api`) — **api 팀 합의 1건**(연구 노트 §2).
3. **ingest 재사용 + glue 교체**: `encode_jpeg` RGB→640×480→JPEG **코어 재사용**(입력만 `rtc.VideoFrame`(RGB24)→
   ndarray로 어댑트). 트랙 glue→`consume_*_lk(stream, windower)`(`async for ev in rtc.Video/AudioStream`).
   ★ **audio는 `AudioStream(track, sample_rate=16000, num_channels=1)`로 받으면 16k mono 직접 공급 →
   `PcmResampler`/`_concat_pcm`/`flush` 불필요**(resample-list gotcha는 aiortc 경로 한정). ts: video=
   `ev.timestamp_us/1e6`, audio=**누적 `frame.duration`**(첫프레임 t0 정규화·poll-clock 유지). 종료=EOS(`async for`
   자연종료, `MediaStreamError` 삭제), `CancelledError` 전파. windowing/emit/recorder/sink **전부 무변경**.
   레이어링: 룸 lifecycle은 media 아닌 **신규 `server/`**에, media는 `consume_*_lk` glue까지(leaf 유지).
   **기존 aiortc `consume_video/consume_audio`는 삭제하지 말고 유지**(핫패스만 LiveKit).
4. ★ 새 dep: **`livekit~=1.1`**(RTC, 1.1.9대; 직접 발급 시 `livekit-api~=1.1`도). **manylinux wheel→컨테이너
   glibc(slim) 확정**(alpine 불가). 검증: async-iterable 더블로 `consume_*_lk` 단위테스트(`test_ingest_lk.py` —
   ts·1fps·duration누적·PCM bytes) + 실 LiveKit은 그쪽 `infra/docker-compose.media.yml`+publisher 스크립트, down이면 skip.
5. 리스크: **로컬 LiveKit 서버 필요**(SDK fake 없음→더블 주입) · **poll-clock 정합** · **이벤트루프 블로킹**
   (critic/STT는 executor; `room.on` 콜백 안 await 금지→`ensure_future`) · **토큰/권한**(hidden·canSubscribe).
   (그 다음 Slice 9=라이브 e2e + 실 critic — 종단 레이턴시·전사 품질 evidence.)

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
