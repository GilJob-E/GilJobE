# NEXT — 새 세션이 가장 먼저 읽는 파일 (고정 진입점)

> SessionStart 훅이 이 파일을 가리킨다. **매 슬라이스 종료 시 이 파일을 다음 슬라이스로 갱신**한다.
> 규칙: `CLAUDE.md` "세션 시작 프로토콜". 전체 빌드 순서: `PLAN.md §7`.

## 지금 상태 (최근 갱신: Slice 9 완료 시점)
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
- **완료: Slice 8** — LiveKit 구독자 입력 어댑터(PLAN §7-8 피벗 구현). 상세 → `.dev/handoffs/slice-08-livekit-subscriber.md`.
  - 신규: `media/ingest.py`에 `consume_video_lk`/`consume_audio_lk`(livekit 미import 덕타이핑 glue, 16k mono 직수신=리샘플 불필요)+`encode_jpeg_rgb`(RGB24 코어). 신규 `server/`(`LiveKitSubscriber`=룸 lifecycle·hidden 구독·late-join 순회·sid dedup·poll 타이머·**worker→loop 브리지**(call_soon_threadsafe→queue→drain, SSE-safe)·aclose) + `token`(hidden self-mint) + `config`(env 토큰/self-mint) + `app.build_subscriber`(조립 seam). windowing/emit/recorder/sink **무변경**.
  - ★★ **통합 스택이 로컬 실행 중 발견**: compose `giljob-v2`(소스 `/home/hoddukzoa/GilJob_v2`, 타user), livekit-server v1.8 @localhost:7880, api가 토큰발급. → 사용자 인가 하에 키 읽어 hidden 토큰 self-mint해 **실 LiveKit 라이브 e2e PASS**(window 3+turn_end 1, 실 RGB24/16k·ICE 통과, 3회). 메모리 `giljob-docker-integration-target` 갱신.
  - **오프라인 93 passed**(기존 73 + ingest_lk 8 + server 12), 플레이크 0/5. LiveKit API는 설치본(1.1.9) introspection 실측(추측 0).
  - ★ 6차원 적대적 리뷰(44 에이전트): 19→14확정→fix-now 1(소비 태스크 수명/관측성) 근본 수정+회귀가드. deferred 12건은 핸드오프에. **최주목 deferred=크로스트랙 t0/poll-clock 정합**(video=프레임 ts, audio=누적 duration 각자 t0 → 트랙 시작 시차 시 nv·stt 오프셋; v1 동시시작 수용, Slice 9에서 **측정 후 닫음**(아래).
- **완료: Slice 9** — 라이브 e2e + 실 critic. 상세 → `.dev/handoffs/slice-09-live-critic.md`.
  - 신규: `tests/test_e2e_live_critic.py`(게이트 1) — **독립 livekit-server:v1.8(dev, --network host)** + kor.mp4 publisher(사전 디코드 360x640 RGB24@10fps + 16k mono, realtime push) → **실 `WindowCritic`+`GemmaNativeTranscriber`**(vLLM E4B) → JSONL 종단. **src 무수정**(계측=테스트 레벨: `_ClockSink` emit시각 + `_InstWindower` 첫 add_* 시각). evidence `tests/evidence/live-critic.json`(판정)+`…raw.json`(수치).
  - ★ **측정 시점 giljob-v2 스택이 내려가 있었음**(원인=의도적 `docker compose down`, Exit 137=SIGKILL·`OOMKilled=false`·메모리 118GB 여유 — **OOM 아님**; 슬라이스 후 `docker start`로 재기동) → 당시엔 깨우지 않고 독립 livekit(통합 동일 v1.8)로 측정. dev 키 devkey/secret(통합 키 아님).
  - 측정(PASS 2런, window 14·eval 2·turn_end 1 동일): **freshness lag** live median 0.76~0.99s/max 1.07(poll지연+추론, 3s cadence 충분) · **전사** kor.mp4 자기소개 verbatim 유지(★ **Opus 왕복**에도; ASR 오류=비결정적 음운 슬립) · **nv** engaged/neutral/nervous 구분(단 긴장류 그라운딩 불가=`[[vision-nonverbal-instability]]` 재확인) · **eot** turn_end(transcript_full+compact eval) OK.
  - ★ **deferred #1 크로스트랙 t0 = 측정으로 닫음**: audio가 video보다 **~0.3s 선행**(LiveKit이 video 늦게 딜리버). 0.3s≪3s라 페어링 유효 → **v1 수용**(공유 wall-clock t0 도입 안 함). 재검토 트리거=윈도우<1.5s 또는 실 브라우저 skew>1s. 파일 publisher라 이 값은 하한(브라우저는 더 클 수 있음).
  - **오프라인 93 무회귀**. 적대적 리뷰=인라인(측정 방법론 자기검증, 정정 버그 0).
  - ★ **후속(Slice 9 종료 후)**: ① giljob-v2 스택 **재기동함(현재 UP)** ② **공유 백엔드로 재검증** — `VLLM_BASE_URL`=스택 gemma-e4b(8000) + LiveKit=스택 livekit(7880, 실 키)으로 재실행 → 독립 구성과 **동일**(window 14·eval 2·turn_end 1, lag ~1s, skew ~-0.37s, 전사 verbatim). analysis-engine이 자체 vLLM 없이 스택 백엔드 공유하는 **실 통합 구성 실증**. 테스트 자격증명 `_resolve_creds`(env→컨테이너→dev fallback)로 일반화. evidence `live-critic.json`에 두 구성·공유 백엔드 발견 반영. 메모리 `giljob-docker-integration-target` 갱신.

- **완료: Slice 10** — 실 브라우저 e2e 스모크 **PASS(2회 재현)**. 상세 → `.dev/handoffs/slice-10-browser-smoke.md`.
  - 신규(`.dev/slice10/`): `smoke_browser_live.py`(구독자 하먼스, _InstWindower/_ClockSink 계측 재사용, src 무수정)·
    `drive_browser_pub.py`(POST /api/sessions → 가짜장치 헤드리스 Chrome으로 pub.html publish, Playwright CDP)·
    `pub.html`(최소 publisher, livekit-client 2.7.5 getUserMedia 동시 enable)·`tail_records.py`. evidence
    `tests/evidence/browser-live.json`(+raw). **오프라인 93 무회귀**(src 미변경).
  - 진짜 헤드리스 Chrome `getUserMedia`(가짜장치 kor.y4m/wav) → livekit-client → publish → 우리 subscriber 종단.
    Slice 9 합성 publisher가 우회한 **#1 갭(브라우저→publish) 닫음**. 전사 verbatim·nv·eval·turn_end.eval 전부 OK.
  - ★★ **통합 스택이 전진 발견**(NEXT.md 위 §통합 타깃 갱신 필요): 다른 팀이 **우리 레포(b769120=현재 src와
    동일)를 클론해 `analysis-engine`(8200) HTTP 래퍼**로 실행 중(`ANALYSIS_ENGINE_ENABLE_SUBSCRIBER=false`), 프론트가
    `/analysis/subscriber/start|stop`·`/analysis/signals` 계약에 완전 배선(턴경계=UI버튼, 출력=프론트가 next-question에 중계).
    → roadmap 오픈질문 #1~3 그쪽 선택으로 닫힘.
  - ★ **blocker 3종(전부 그쪽 인프라, 적대검증 holds)**: (a) 번들 SDK 2.19.1↔서버 1.8.4 비호환(/rtc/v1 404) → 2.7.5로 우회,
    (b) ai-engine Gemini 429(크레딧 소진)로 마이크 게이팅 차단 → 최소 pub.html로 UI 우회, (c) LiveKit node_ip=127.0.0.1·
    use_external_ip=false → 원격 브라우저 ICE 실패 → 호스트 내부 Chrome으로 수행(사람 웹캠 길 B는 보류).
  - ★ **deferred #1 크로스트랙 t0 = 실 브라우저로 닫음**: 동시 enable 클린 런 skew **−0.282s**(LiveKit이 video ~0.28s 늦게
    딜리버, publish 순서 아님). <1s → v1 수용. Slice 8/9 deferred #1 라인 **종결**.
  - ★ ultracode 적대 검증(워크플로 4 검증자): browser-path·stack-findings HOLDS, measurements·completeness는
    NUANCED → **클린 재실행으로 해소**(turn_end.eval=None=타이밍 아티팩트 확정, 크로스트랙 confound 제거).

## ★★ 통합 타깃 (모든 다음 슬라이스가 인지 — 메모리 `giljob-docker-integration-target`)
이 레포는 최종적으로 **`github.com/GilJob-E/giljob-docker`의 `services/analysis-engine/`**로 통합된다.
입력 모델이 PLAN과 다름: aiortc 직접 수신이 아니라 **LiveKit room subscribe**. **그쪽 프론트(`apps/web`)는
이미 LiveKit으로 publish 완성**(룸 `giljob-session-{id}`, 토큰 `POST /api/sessions`)이고, **브라우저 STT를
의도적으로 제거해 전사를 우리한테 위임**(최신 커밋 "Route transcription through GilJobE analysis engine").
→ 우리는 같은 룸에 **subscriber로 join → candidate track 구독 → 파이프라인 → transcript+signal emit**.
출력 소비자=`services/ai-engine`(Gemini, HTTP+JSON, internal). 컨테이너 규약(alpine→네이티브 시 slim
협의·requirements.txt·/healthz·AGENTS+CLAUDE 쌍)은 메모리 참조.

## 다음 할 일 = Slice 11: **통합 계약 실 검증 — 그쪽 analysis-engine `ENABLE_SUBSCRIBER=true`** ⭐
> ★ roadmap(`roadmap-browser-to-pr.md`)은 Slice 10 발견(통합 스택 전진)으로 **부분 superseded** — slice-10
> 핸드오프 §"통합 스택 전진" + §"다음 슬라이스 후보"를 정본으로. 핫패스(입력→비평+전사→JSON)는 합성·실
> critic·라이브·실 브라우저까지 **전부 닫혔다**. 남은 건 그쪽 계약 충족 + 실 통합 검증.

1. 먼저 **`slice-10-browser-smoke.md`**(통합 스택 전진·계약·blocker 3종) + 메모리 `giljob-docker-integration-target` 정독.
2. **환경 확인**: `docker ps`로 giljob-v2 스택 UP. analysis-engine-1(8200)이 **우리 레포 b769120(=현재 src 동일)**
   를 클론해 실행 중이나 `ANALYSIS_ENGINE_ENABLE_SUBSCRIBER=false`. **우리 vLLM/livekit 따로 안 띄움**(스택 공유).
3. **실 통합 검증**(핵심): 그쪽 `ENABLE_SUBSCRIBER=true`로 켜고(그쪽 env/이유 조율 필요 — 왜 꺼뒀나) **전 루프**
   확인 — 브라우저 publish → 그쪽 analysis-engine이 우리 subscriber 띄움 → `GET /analysis/signals`에 우리
   transcript/nv → 프론트가 `/ai/interview/next-question`에 중계. (계약: `/analysis/subscriber/start|stop`·`/analysis/signals`,
   룸 `giljob-session-{sessionId}`, POST /api/sessions가 sessionId 존중.)
4. **그쪽에 blocker 보고**(코드보다 먼저): (a) 번들 SDK livekit-client 2.19.1 ↔ 서버 1.8.4 비호환(/rtc/v1 404 churn —
   그쪽 프론트 publish도 영향), (b) ai-engine Gemini 크레딧 소진(429), (c) LiveKit node_ip=127.0.0.1·use_external_ip=false
   (원격 브라우저 ICE 실패 — external IP/TURN 또는 ICE-TCP).
5. (부수) 사람 웹캠(길 B): 터널 7881(ICE-TCP) 추가 또는 스택 node_ip=Tailscale IP 후 재시도. / 스모크 하먼스
   (`.dev/slice10/`)를 tests/ 게이트 e2e로 승격 검토.

> 환경: 스택 **현재 UP**. **우리 vLLM/livekit-dev 띄우지 말 것**(스택 gemma-e4b 8000·livekit 7880 공유). GPU
> 위생: 스택 gemma-e4b가 GPU 사용 중(정상, 스택 소유 — 우리가 정리할 컨테이너 없음).

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
