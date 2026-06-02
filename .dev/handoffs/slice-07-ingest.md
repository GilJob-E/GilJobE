# Handoff — Slice 7: ingest (media/ingest.py) (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md §7-8`(server)·§8(브라우저) 순으로 읽는다.
> 전체 빌드 순서: `PLAN.md §7`.

## 결론 (DONE)

aiortc **트랙 소비자**를 신규 작성: video 트랙 → ~1fps throttle → 640×480 JPEG bytes, audio 트랙(Opus
48kHz) → 16kHz mono Int16 PCM bytes. 각 청크에 스트림 시작 기준 타임스탬프(초)를 붙여 윈도워의
`add_frame/add_audio`로 투입한다(이벤트루프 비차단). PyAV API는 **설치된 av 16.1.0/aiortc 1.14.0에서
실측 검증**(메모리 아닌 실제 호출)하고, ★ **resample-list gotcha**를 코드와 테스트로 직접 잠갔다.
**오프라인 73 passed**(기존 61 + ingest 12), 플레이크 0/30. *GPU/브라우저 불요.* 인라인 적대적
리뷰 통과(정정 버그 0, 실제 커버리지 갭 3건 보강).

## 무엇을 만들었나

```
src/giljobe/media/ingest.py        (신규)  트랙 소비자 + 순수 변환
tests/test_ingest_slicing.py       (신규, 오프라인) 12 테스트 + 합성 PyAV 프레임/더블
pyproject.toml                     (수정)  deps에 aiortc~=1.14·av·numpy·Pillow 추가(설치됨)
```

### ingest.py 설계 — "순수 변환 ↔ 얇은 async glue" 분리(★ 통합 seam)
- **순수 변환**(트랙·이벤트루프 불요 → 단위 테스트 직격):
  - `encode_jpeg(frame, *, size=(640,480), quality=85)` — `frame.reformat(640×480, rgb24)`(swscale 스케일)
    → `to_image()`(PIL) → JPEG. **cv2 시스템 의존 회피**(PLAN). yuv420p 등 비-rgb24 입력도 reformat이 흡수.
  - `class PcmResampler` — `av.AudioResampler(format="s16", layout="mono", rate=16000)` 1개를 스트림 내내
    재사용(프레임 경계 샘플 이어붙임). `resample(frame)→bytes`(★ list 순회+concat), `flush()→bytes`(꼬리 배출).
  - `_concat_pcm(frames)` — `to_ndarray().tobytes()` join(빈 list→b"").
- **async 소비자**(얇은 glue):
  - `consume_video(track, windower, *, target_fps=1.0, size, quality)` — `await track.recv()` 루프, throttle
    (emit 시 `emit_at=rel+interval`로 재앵커 → **누적 드리프트 없음**), `add_frame(rel, jpeg)`.
  - `consume_audio(track, windower, *, sample_rate=16000)` — recv 루프, `add_audio(rel, pcm)`, **종료 시 flush**로 꼬리 배출.
  - 둘 다 `except MediaStreamError`(트랙 종료)만 잡고 **`CancelledError`(취소)는 전파**(MediaStreamError⊂Exception,
    CancelledError⊂BaseException — 동작으로 검증). 타임스탬프=`frame.time`(pts*time_base)를 첫 프레임 t0로 정규화(트랙별).
- **레이어링**: media는 **leaf** — TurnWindower를 import하지 않고 구조적 `_Windower` Protocol에만 의존
  (의존 방향 단방향 유지; grep로 pipeline/analysis import 0 확인).
- **비차단**: 변환은 ms급(1fps 인코드·~20ms 리샘플)이라 코루틴 직접 수행. **추론(critic/STT 3–7s)은
  ingest에서 호출 안 함** — 윈도워 executor 경유(PLAN 최대 실패모드 회피).

### 12 테스트 (합성 PyAV 프레임, GPU/브라우저 불요)
1. **resample()=list + 작은 입력=빈 list** — 3-샘플 입력→`[]`(→`[0]`은 IndexError = 미순회 코드가 깨지는 지점).
2. **순회+flush 샘플 보존** — 8프레임 48k stereo→정확히 K*320 샘플, 각 청크 Int16 정렬(%2==0).
3. **flush 생략=꼬리 누락** — flush 빼면 필터 지연 꼬리 손실을 직접 assert(flush load-bearing).
4. **16k mono(3:1)** · 5. **mono 입력**(레이아웃 가정 비의존) · 6. **JPEG 640×480** · 7. **yuv420p 웹캠 포맷**.
8. **1fps throttle** — 64프레임@30fps→t=0,1,2 셋만(전부 640×480) · 9. **pts 없음 fallback**.
10. **audio 청크 투입+flush 꼬리**(총 PCM 보존·정렬·t 단조증가) · 11. **빈 트랙 종료** · 12. **취소 전파**.

## ★ 적대적 리뷰 (인라인 — 워크플로 정체로 직접 수행)

5차원(PyAV정확성/이벤트루프블로킹/타임스탬프동기/커버리지갭/레이어링) 적대적 리뷰를 `.venv` 실증으로
수행. **fix-now 정정 버그 0건** — 견고함을 실측 확인: 레이어 clean·취소 안전·mono/yuv420p/16k-passthrough/
빈입력/큰입력 정상·resample+flush·throttle 드리프트 없음·PCM 정렬. **실제 커버리지 갭 3건 보강**(mono
입력·yuv420p·취소 전파 — 기존 테스트는 stereo s16 + rgb24만 썼음). note-only는 아래 "다음 슬라이스"로 위임.
> (참고: 별도 워크플로 에이전트 리뷰를 시도했으나 review→verify 전환에서 정체 → 정리하고 인라인 수행.)

## 검증 결과 (PASS)
- `pytest tests/test_ingest_slicing.py` → **12 passed**. 30회 반복 **0 실패**.
- 전체 오프라인(live/spike 제외) → **73 passed**(기존 61 무회귀 + ingest 12).
- PyAV API 실측 검증(av 16.1.0): AudioResampler ctor·resample()=list·작은입력=빈list·flush 꼬리·
  reformat/to_image·frame.time(pts*time_base)·MediaStreamError 계층 — 전부 실제 호출로 확인(추측 0).
- vLLM/GPU 불요 → 라이브 게이트 없음, 오프라인 통과가 완료 기준(PLAN §6 ingest 항목 충족).

## ★ 다음 슬라이스로 넘기는 note(설계 결정 — 버그 아님, Slice 8/server가 처리)
- **poll-clock 계약(중요)**: ingest는 프레임을 **미디어 상대초**(첫 프레임=t=0)로 태깅한다. 서버(Slice 8)가
  `poll(now)`를 **같은 타임라인**으로 몰아야 윈도우 경계가 맞는다(예: `loop.time()-t0_wall` 또는 최근 프레임 rel).
  ingest는 poll/turn 경계를 구동하지 않는다(단일 책임 — poll은 프레임이 끊겨도 떠야 하므로 서버 타이머 몫).
- **크로스트랙 t0**: video/audio가 각자 첫 프레임으로 t0 정규화 → 두 트랙이 다른 미디어 시각에 시작하면
  nv·stt 페어링이 그만큼 오프셋. getUserMedia는 ~동시 시작이라 v1(단일 세션) 수용. 엄밀 동기 필요 시
  서버가 공유 t0 주입. **(통합 시 LiveKit로 입력 모델이 바뀌므로 어차피 재검토 — 메모 참조.)**
- **audio 청크 경계**: ~20ms 청크는 시작 t로 통째 한 윈도우에 담김(≤20ms 경계 효과, gje 관용·수용).

## ★★ 통합 타깃 (반드시 인지 — 메모리 `giljob-docker-integration-target`)
이 레포는 최종적으로 **`github.com/GilJob-E/giljob-docker`의 `services/analysis-engine/`**(예약된 이름)로
들어간다. **입력 모델이 다르다**: 우리 PLAN의 "aiortc 직접 수신"이 아니라 **LiveKit room에 subscribe
participant로 join해 track 구독**. → 이번 ingest의 **순수 변환·windowing 코어는 재사용**되고, 트랙 소비
glue(`consume_video/consume_audio`의 `track.recv()`+`MediaStreamError`)만 **LiveKit SDK
(`rtc.VideoStream/AudioStream`)로 교체**하면 된다(설계가 그 seam으로 분리돼 있음). 출력 소비자는
`services/ai-engine`(Gemini, HTTP+JSON, internal). 컨테이너 규약(alpine→네이티브 의존 시 slim 협의·
requirements.txt·/healthz·AGENTS+CLAUDE 쌍)은 메모리 참조. **Slice 8 server 설계 시 aiortc-직접 vs
LiveKit-subscribe를 의식**할 것(데모는 aiortc로 진행해도, 통합 어댑터를 염두).

## 다음 슬라이스 = Slice 8: server + 브라우저 — PLAN §7-8
- `server/app.py`: aiohttp — `POST /offer`(SDP 협상), `GET /signals`(SSE, emit/sink 연결), `/turn/start|end`,
  정적 `web/`. **poll 타이머** 소유(위 poll-clock 계약). ingest `consume_video/consume_audio`를 트랙별 태스크로 띄움.
- `web/index.html`: getUserMedia → RTCPeerConnection → POST /offer, `/signals` EventSource tail(데모 가시화).
- 새 dep: **aiohttp**(server 몫, 아직 미설치 — pyproject 주석대로). 검증: `http://localhost`(secure context) 수동 스모크.
- (그 다음 Slice 9 = 라이브 e2e: vid_0001/0033 + 실 critic, evidence JSON.)

## git
이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안:
`feat(slice7): media/ingest.py — aiortc 트랙 소비자(video→1fps JPEG, audio→16k mono PCM) + resample-list gotcha 테스트; deps(aiortc/av) 추가; 인라인 적대적 리뷰(mono/yuv420p/취소 커버리지 보강)`.
- 커밋 대상: `pyproject.toml`·`src/giljobe/media/ingest.py`·`tests/test_ingest_slicing.py`·`.dev/handoffs/slice-07-ingest.md`·`NEXT.md`.
- **제외**: `.dev/`의 파킹된 vision 탐색 스크래치(`frame_*.jpg`/`pyfeat_*`/`vision_grounding_*`/`pose_grounding_*`) — 미관련(slice-06 관례 유지).
