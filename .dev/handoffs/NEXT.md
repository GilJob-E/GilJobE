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
  - **오프라인 93 무회귀**. 적대적 리뷰=인라인(측정 방법론 자기검증, 정정 버그 0). GPU 위생: 종료 시 `docker stop vllm-gemma4-e4b livekit-dev`+`nvidia-smi`.

## ★★ 통합 타깃 (모든 다음 슬라이스가 인지 — 메모리 `giljob-docker-integration-target`)
이 레포는 최종적으로 **`github.com/GilJob-E/giljob-docker`의 `services/analysis-engine/`**로 통합된다.
입력 모델이 PLAN과 다름: aiortc 직접 수신이 아니라 **LiveKit room subscribe**. **그쪽 프론트(`apps/web`)는
이미 LiveKit으로 publish 완성**(룸 `giljob-session-{id}`, 토큰 `POST /api/sessions`)이고, **브라우저 STT를
의도적으로 제거해 전사를 우리한테 위임**(최신 커밋 "Route transcription through GilJobE analysis engine").
→ 우리는 같은 룸에 **subscriber로 join → candidate track 구독 → 파이프라인 → transcript+signal emit**.
출력 소비자=`services/ai-engine`(Gemini, HTTP+JSON, internal). 컨테이너 규약(alpine→네이티브 시 slim
협의·requirements.txt·/healthz·AGENTS+CLAUDE 쌍)은 메모리 참조.

## 다음 할 일 = Slice 10: **통합 패키징** (services/analysis-engine 컨테이너화) — 또는 견고성 잔여
> 핫패스(입력→비평+전사→JSON)는 Slice 9까지 **실 critic 라이브로 닫힘**. 남은 일은 통합 타깃으로의
> 컨테이너 패키징 + 출력 소비자(ai-engine) 연결. 핵심 결정 일부는 api 팀 합의가 필요(아래 §미합의).

1. 먼저 **`.dev/handoffs/slice-09-live-critic.md`**(측정 방법론·deferred#1 결정·환경 기동법) +
   `slice-08-livekit-subscriber.md`(deferred 목록·미합의) + 메모리 `giljob-docker-integration-target` 정독.
2. **통합 패키징**: `services/analysis-engine/` 규약(메모리 참조) — slim base(alpine→네이티브 협의)·
   `requirements.txt`(pyproject deps 추출)·`/healthz`·`AGENTS.md`+`CLAUDE.md` 쌍·내부포트. 진입점=
   `server.app.build_subscriber(WindowCritic(default_vllm_client()), [JSONLSink, WebhookSink(ai-engine)], SubscriberConfig.from_env(...))` → `await sub.run(url, token)`.
3. **출력 소비자 연결**: `services/ai-engine`(Gemini, HTTP+JSON, internal)로의 송출 = `WebhookSink` 추가
   (PLAN §4 (c); 지금은 SSE+JSONL만). 실 URL/스키마 합의 후 `emit/sink.py`에 추가(per-window 비용·재시도·
   순서 — emit는 models만 의존 유지).
4. **api 팀 합의(미합의 잔여)**: hidden subscriber **토큰 발급 경로** — services/api가 발급(권장, 키 거기)
   vs 우리 self-mint. 코드는 양쪽 지원(`resolve_token`). 룸 규약=`giljob-session-{id}` 확인.
5. (선택) 견고성 deferred: slice-08 #4(멀티턴 시 executor `wait=True`/future cancel), #5(JSONLSink fd
   누수), **노이즈/다화자 전사 재확인**(Slice 9는 단일 화자 스크립트 클립만). 실 브라우저 크로스트랙
   skew 측정(>1s면 공유 t0 도입)은 giljob-v2 풀스택 필요 — deferred#1 재검토 트리거.

> 환경 기동(라이브 재현): vLLM `bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`(HF_TOKEN env) +
> 독립 livekit `docker run -d --name livekit-dev --network host livekit/livekit-server:v1.8 --dev`.
> **GPU 위생**: 작업 끝 `docker stop vllm-gemma4-e4b livekit-dev` + `nvidia-smi`로 VRAM 해제 확인.

## 불변 규칙 (매 슬라이스 공통)
- src = 계층형 subpackage. import: **intra=relative / cross=absolute** (PLAN.md §1, 메모 `[[layered-src-structure]]`).
- gje 원본(`/home/kio/workspace/gje`)은 **복사만**, 이동/수정 금지. FS·데이터가 예상과 다르면 **멈추고 보고**(이번 슬라이스 영어클립 사건처럼).
- 되돌리기 어려운 작업(삭제/외부발송)은 **승인 후**. 시크릿(`HF_TOKEN`) 출력/커밋 금지. 커밋+푸시는 작업단위 끝 습관(메모 `[[commit-habit]]`).
- 완료 기준은 측정 가능하게(pytest 통과 + 실 vLLM 검증), 충족까지 반복.
- 슬라이스 끝: `.dev/handoffs/slice-NN-*.md` 아카이브 작성 + **이 NEXT.md 갱신**.
