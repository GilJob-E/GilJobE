# Handoff — Slice 8: LiveKit 구독자 입력 어댑터 (완료)

> 슬라이스마다 `/clear`. 다음 세션은 `NEXT.md` → 이 문서 → `PLAN.md`(단, §7-8은 이 피벗으로 일부
> superseded — 본 문서/NEXT.md 우선) 순으로 읽는다.

## 결론 (DONE)

통합 타깃(`GilJob_v2`)의 입력 모델인 **LiveKit room subscribe**로 ingest 핫패스를 피벗 구현했다. 우리는
`giljob-session-{id}` 룸에 **hidden participant**로 join → candidate audio+video track 구독 →
ingest(`consume_*_lk`) → windower/recorder/sink(전부 **무변경 재사용**) → JSONL 레코드 송출.
**오프라인 93 passed**(기존 73 + ingest_lk 8 + server 12), 플레이크 0/5. ★ **실 LiveKit 라이브 e2e
PASS**(로컬 `giljob-v2` livekit-server v1.8에 실제 붙어 publisher RGB24 영상+16k 오디오 → 우리
파이프라인 → JSONL window 3 + turn_end 1; 3회 안정). LiveKit API는 설치본(1.1.9) introspection으로
실측(추측 0). 6차원 적대적 리뷰(44 에이전트) — fix-now 1건(소비 태스크 수명/관측성) 근본 수정 + 회귀
가드, deferred 12건은 아래 명시.

## ★★ 환경 발견 (반드시 인지 — 메모리 `giljob-docker-integration-target` 갱신함)

**통합 타깃 스택 전체가 로컬에 실행 중**이었다(예상 경로 `giljob-docker`는 없음):
- docker-compose 프로젝트 **`giljob-v2`**, 소스 `/home/hoddukzoa/GilJob_v2/`(**다른 사용자 홈** — kio
  직접 읽기 어려움; 접근면은 localhost 포트).
- `giljob-v2-livekit-1`(**livekit/livekit-server:v1.8**, `localhost:7880/7881`+UDP 50000-50100) ·
  `giljob-v2-api-1`(토큰 발급, env `LIVEKIT_API_KEY/SECRET/URL/...`) · `web`(3000) · `ai-engine`(8100,
  다운스트림 소비자) · `coturn` · `caddy` · `postgres`.
- → 실 LiveKit 구독 e2e가 로컬에서 가능. **사용자 인가** 하에 api 컨테이너 env에서 키를 읽어 hidden
  subscriber 토큰을 self-mint해 라이브 e2e 수행(값 출력/커밋 안 함). `livekit==1.1.9` pip 설치.

## 무엇을 만들었나

```
src/giljobe/media/ingest.py      (수정)  LiveKit glue 추가(기존 aiortc 함수 유지)
src/giljobe/server/__init__.py   (신규)  빈 패키지
src/giljobe/server/token.py      (신규)  subscriber_token — hidden subscriber JWT self-mint
src/giljobe/server/config.py     (신규)  SubscriberConfig.from_env + resolve_token(env 토큰/self-mint)
src/giljobe/server/subscriber.py (신규)  LiveKitSubscriber — 룸 lifecycle + 디스패치 + poll + 브리지
src/giljobe/server/app.py        (신규)  build_subscriber 팩토리(windower+recorder+subscriber 조립)
tests/test_ingest_lk.py          (신규, 오프라인) 8 — consume_*_lk 더블 직격
tests/test_server_subscriber.py  (신규, 오프라인) 12 — fake Room/stream 오케스트레이션 + 실 브리지 e2e
tests/test_e2e_live_lk.py        (신규, 게이트)   1 — 실 LiveKit publish→subscribe→JSONL
pyproject.toml                   (수정)  livekit~=1.1 · livekit-api~=1.1 추가
```

### ingest.py — "순수 변환 ↔ glue" seam 재사용, LiveKit 경로 추가
- `encode_jpeg_rgb(data, w, h, *, size, quality)` — RGB24 raw 버퍼 → 640×480 JPEG(PIL resize; aiortc
  `encode_jpeg`의 PyAV reformat 대응물). tight 패킹 가정 + 행 stride 패딩 흡수 분기.
- `consume_video_lk(stream, w)` / `consume_audio_lk(stream, w)` — **livekit 미import 덕타이핑**(`.frame/
  .timestamp_us/.duration/.data`만 사용 → media=leaf 유지, 더블로 오프라인 테스트). 차이: 종료=EOS
  (`async for` 자연 종료, `MediaStreamError` 없음) · audio는 `AudioStream(16k,mono)` 직수신이라
  **PcmResampler/flush 불필요** · video ts=`ev.timestamp_us/1e6`(첫프레임 t0=0), audio ts=**누적
  `frame.duration`**(AudioFrameEvent엔 ts 없음 — 실측). 취소(CancelledError)는 전파, finally에서 `aclose`.

### server/ — 룸 lifecycle은 여기(media는 leaf 유지)
- `LiveKitSubscriber`: `start()`(브리지·핸들러·드레인/poll 태스크·턴 reset) → `connect(url, token)`(실
  접속 + **기존 publish track 순회 구독**: 늦은 join 시 `track_subscribed` 안 와서) → `aclose(now)`(턴
  flush + 정리). `run(url, token)`=한 번에 + disconnected 대기.
  - **트랙 디스패치**: `track_subscribed`(동기 콜백) → `create_task(consume_*_lk(VideoStream/AudioStream))`.
    `track.kind`로 분기(KIND_VIDEO=2/AUDIO=1 상수 — server가 import 시 livekit 강제로딩 안 하게). **sid
    dedup**(콜백↔순회 중복 방지). 첫 track이 **poll-clock t0** 앵커.
  - **worker→loop 브리지(★ PLAN §5)**: `make_signal_bridge`=윈도워 `on_signal`이 `call_soon_threadsafe
    (queue.put_nowait)`로만 마샬링 → `_drain_loop`이 루프 스레드에서 큐 비워 `recorder.on_signal` 호출.
    그래서 recorder/sink가 **전부 루프 스레드에서** 돌아 비스레드세이프 sink(SSE StreamResponse) 안전.
  - **poll-clock**: `_poll_loop`이 `poll_interval`마다 `windower.poll(loop.time()-t0)`. 타이머 구동이라
    프레임이 끊겨도(침묵) 완성 윈도우를 flush. t0=첫 track 구독 시각(프레임 도착 직전 근사).
  - **aclose 순서**: poll cancel → `end_turn`(executor, 드레인 동시 기록) → drain cancel → `_flush_queue`
    (남은 turn_end 동기 배출) → `windower.close` → 소비 태스크 cancel → `room.disconnect`. 멱등(`_started`).
- `token.subscriber_token` — `AccessToken.with_grants(VideoGrants(room_join, room, can_subscribe=True,
  can_publish=False, hidden=True))`. `config.resolve_token` — env `LIVEKIT_TOKEN` 우선, 없으면
  `LIVEKIT_API_KEY/SECRET`로 self-mint, 둘 다 없으면 None. **시크릿 위생**: config repr에서 token 제외.
- `app.build_subscriber(critic, sinks, config, *, room=None, executors=None)` — 통합 seam(실서비스=
  WindowCritic+JSONLSink, 라이브 e2e=MockCritic+JSONLSink 같은 배선). room 미주입 시 실 `rtc.Room()`.
  ★ `rtc.Room()`은 **실행 중 루프 안에서** 생성해야(동기 본문 생성 시 다른 루프 바인딩=disconnect 깨짐).

## 검증 결과 (PASS)
- 오프라인 `pytest`(live/spike 제외) → **93 passed**(기존 73 무회귀 + ingest_lk 8 + server 12). 플레이크 0/5.
- 라이브 e2e `tests/test_e2e_live_lk.py` → **PASS 3회**(window 3 + turn_end 1, 실 RGB24/16k 미디어 흐름·
  ICE 통과·전사 합본). 게이트: 키(env 또는 로컬 api 컨테이너) + 서버 7880 도달, 미충족 시 skip.
- LiveKit API 실측(설치 1.1.9): `VideoBufferType.RGB24=4`, `TrackKind KIND_AUDIO=1/VIDEO=2`,
  `VideoFrameEvent(frame,timestamp_us,...)`/`AudioFrameEvent(frame)`(ts 없음), `AudioStream(sr=16000,
  num_channels=1)`=16k mono 직공급, `AccessToken.with_ttl(timedelta)`+grant 직렬화 — 전부 introspection/
  JWT 디코드로 확인.
- 레이어링 직접 확인(grep): media/ingest.py에 livekit/pipeline/server import 0(주석만), server는
  cross=absolute·intra=relative 규약 준수.

## ★ 적대적 리뷰 (워크플로 6차원 × 회의론자 2 = 44 에이전트)
19 발견 → real 과반 14 → **fix-now 1**. doer 재판정으로 **소비 태스크 수명/관측성 1묶음 근본 수정**:
- fix-now("aclose 반복 중 동시수정")의 *문자 메커니즘은 거짓양성*(asyncio 단일스레드, for-루프 내 await
  없음 → `_on_track` 콜백 끼어들 수 없음). 하지만 *실제 잠재 우려*(종료 후 늦은 track → orphan 소비
  태스크)는 진짜 → **수정**: `_on_track`에 `if not self._started: return` 가드 + aclose 스냅샷-후-비우기
  + 소비 태스크 done-callback이 **예외 로깅**(finding #2 "silent drop 방지"도 동반 해결). 회귀 가드:
  `test_late_track_after_aclose_is_ignored`.

### deferred (리뷰 다수결대로 — v1 단일세션 범위/YAGNI; Slice 9 견고성·통합에서)
1. **(가장 주목) 크로스트랙 t0 / poll-clock 정합**: video는 첫 video 프레임 ts로, audio는 첫 audio
   프레임부터 누적 duration으로 각자 t0=0 정규화 → 두 트랙이 다른 미디어시각에 시작하면 nv·stt 페어링
   오프셋(AudioFrameEvent에 ts 없어 audio를 video 미디어시계에 직접 못 맞춤). v1=getUserMedia 동시
   시작이라 수용(라이브 e2e 클린 페어링 확인). 근본 해결은 양 소비자에 **공유 wall-clock t0** 주입인데
   server→media 결합/복잡도 ↑ → **Slice 9에서 실 브라우저 candidate로 오프셋을 먼저 측정 후 결정**(실측
   우선). slice-07 핸드오프 "크로스트랙 t0"와 동일 결론.
2. `consume_*_lk`/`encode_jpeg_rgb` 입력 검증 부재(무효 buffer/duration). 실 LiveKit은 유효 frame 보장,
   비정상 buffer는 numpy reshape가 **loud raise**(침묵 손상 아님). 검증 레이어는 통합 시 일괄.
3. `_pcm_bytes` 비-memoryview 경로·`sid=None` dedup·poll-loop "첫 track 전 no-op" — 테스트 갭(실
   LiveKit은 memoryview/유효 sid 공급, 라이브 e2e가 happy-path 보증). mock 테스트 추가는 신뢰 증분 적음.
4. `windower.close()`의 `shutdown(wait=False)`가 미완 eval 스레드 방치(메모리누수 X — `_closed`로 emit
   차단; v1 프로세스 종료 OK). 멀티턴 통합 시 `wait=True`/future cancel.
5. JSONLSink write 예외 시 fd 누수(호출자 close() 계약; v1 단일세션 OK).

## ★ 다음 슬라이스 = Slice 9: 라이브 e2e + 실 critic (종단 레이턴시·전사 품질)
- 지금까지 라이브 e2e는 **MockCritic**(LiveKit 전송·배선 검증 전용). Slice 9는 **실 vLLM WindowCritic +
  GemmaNativeTranscriber**로 종단(candidate 영상→signal/transcript emit) **레이턴시·전사 품질** evidence 측정.
- 실 candidate 입력: (a) 로컬 `giljob-v2` 프론트(브라우저 getUserMedia publish)에 우리가 붙거나, (b)
  publisher가 `kor.mp4`(repo 루트, 한국어, gitignore) 미디어를 push. 토큰: api 발급 vs self-mint.
- ★ deferred #1(크로스트랙 t0) **측정**: 실 브라우저 publish에서 video/audio 첫 프레임 도착 시차를 재고,
  nv·stt 윈도우 정렬이 깨지는지 확인 후 필요하면 공유 t0 도입.
- vLLM 기동(미기동 상태): `bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`, health 게이트 skip.
- (선택) 통합 준비: `services/analysis-engine` 컨테이너 규약(slim base·requirements.txt·/healthz·AGENTS+
  CLAUDE 쌍) — 메모리 `giljob-docker-integration-target`.

## 미합의/오픈 (api 팀과)
- 토큰 발급 경로: services/api가 **hidden subscriber 토큰**을 발급해줄지(권장 — 키 거기 있음) vs 우리
  self-mint(키 공유). 코드는 양쪽 지원(`resolve_token`). 라이브 e2e는 인가 하에 self-mint로 수행 중.

## git
이 슬라이스 커밋(메모 `[[commit-habit]]`). 제안:
`feat(slice8): LiveKit 구독자 입력 어댑터 — media/consume_*_lk(16k 직수신) + server/(룸 lifecycle·poll·worker→loop 브리지·토큰) + 실 LiveKit 라이브 e2e PASS; 적대적 리뷰(소비 태스크 수명/관측성 근본수정)`.
- 커밋 대상: `pyproject.toml`·`src/giljobe/media/ingest.py`·`src/giljobe/server/*`·`tests/test_ingest_lk.py`·
  `tests/test_server_subscriber.py`·`tests/test_e2e_live_lk.py`·`README.md`·이 핸드오프·`NEXT.md`.
- **제외**: `.dev/`의 파킹된 vision 탐색 스크래치(`frame_*.jpg`/`pyfeat_*`/`vision_grounding_*`/
  `pose_grounding_*`/`combined_latency.*`) — 미관련(slice-06/07 관례 유지).
