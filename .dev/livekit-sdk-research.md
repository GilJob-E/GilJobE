# LiveKit Python SDK 조사 — aiortc→LiveKit ingest 교체 (Slice 8 근거)

> 2026-06-02 조사. 모든 API는 **실제 다운로드한 wheel 소스 직독**으로 검증(`livekit==1.1.9`,
> `livekit-api==1.1.0`) + context7/PyPI 교차확인. 추측 없음. 배경: 통합 타깃 프론트가 LiveKit publish
> 완성 + 브라우저 STT를 우리한테 위임 → Slice 8을 **LiveKit 구독자**로 피벗(메모리 `giljob-docker-integration-target`).

## 패키지·버전
| 패키지 | 역할 | 버전 | import | 비고 |
|---|---|---|---|---|
| **`livekit`** | RTC 클라이언트(join/subscribe/frame) — 핵심 | **1.1.9** | `from livekit import rtc` | numpy≥1.26(충돌 없음). **manylinux 바이너리 wheel(Rust FFI)** → **alpine 불가, glibc/slim 베이스 필요** |
| **`livekit-api`** | 서버측 AccessToken/JWT 발급 | **1.1.0** | `from livekit import api` | 우리가 토큰 직접 발급할 때만 |

- 브라우저 `livekit-client@2.19.1`과는 **server-client protocol version(현재 9)**으로 negotiate → `livekit` 1.1.x 호환. `requires-python>=3.9`(3.12 OK).

## 룸 join (hidden 구독자)
```python
from livekit import rtc
room = rtc.Room()
await room.connect(url, token, rtc.RoomOptions(auto_subscribe=True))  # ws(s)://, JWT
# 종료: await room.disconnect()
```
토큰 grant(서버측 `livekit-api`):
```python
from livekit import api
token = (api.AccessToken(KEY, SECRET)
    .with_identity("giljobe-analysis")        # candidate와 다른 고유 identity
    .with_grants(api.VideoGrants(
        room_join=True, room="giljob-session-{id}",
        can_subscribe=True, can_publish=False, can_publish_data=False,
        hidden=True))                          # ★ 다른 참가자에 안 보이는 봇
    .to_jwt())
```
- **토큰 발급 경로(미합의 1건)**: (A) 그쪽 `services/api`가 hidden 토큰 추가 발급(**권장** — 키가 거기 있음) vs (B) 우리가 `LIVEKIT_API_KEY/SECRET` env로 직접 발급(키 공유 필요). 둘 다 기술적 OK → **api 팀과 합의**.

## track 구독·프레임 (핵심)
이벤트: `track_subscribed`/`track_unsubscribed`/`participant_disconnected`/`disconnected`. **콜백은 동기** → async는 `asyncio.ensure_future`로.
```python
@room.on("track_subscribed")
def _(track, publication, participant):
    if track.kind == rtc.TrackKind.KIND_VIDEO:
        asyncio.ensure_future(consume_video_lk(rtc.VideoStream(track, format=rtc.VideoBufferType.RGB24), windower))
    elif track.kind == rtc.TrackKind.KIND_AUDIO:
        asyncio.ensure_future(consume_audio_lk(rtc.AudioStream(track, sample_rate=16000, num_channels=1), windower))
```
- ★ **늦게 join하면 기존 track엔 `track_subscribed` 안 옴** → `room.remote_participants` / `participant.track_publications` 순회해 직접 `VideoStream(pub.track)` 시작.
- **video** `rtc.VideoStream(track, format=RGB24)` → `async for ev`: `ev`=**VideoFrameEvent**(`.frame`, **`.timestamp_us`**, `.rotation`). `VideoFrame`: `.width/.height/.type/.data(memoryview)`, `.convert(VideoBufferType.RGBA…)`. 포맷값: RGBA/BGRA/ARGB/ABGR/**RGB24**/I420/NV12…
- **audio** `rtc.AudioStream(track, sample_rate=16000, num_channels=1)` → FFI가 **16k mono int16 직접 공급**. `async for ev`: `ev`=**AudioFrameEvent**(`.frame`만, **timestamp 없음**). `AudioFrame`: `.data(memoryview int16, `.cast("h")`)`, `.sample_rate/.num_channels/.samples_per_channel/.duration`, `.to_wav_bytes()`. 문서: "int16 interleaved by channel".
- 종료: EOS → `async for` 자연 종료(예외 아님). 명시 정리 `await stream.aclose()`.

## 우리 PyAV 변환과 매핑
- **Audio: `PcmResampler`/`_concat_pcm`/`flush`/`_AUDIO_FALLBACK_DUR` 전부 불필요** — LiveKit이 16k mono를 직접 줌. `add_audio(t, frame.data.cast("B").tobytes())`. (resample-list gotcha는 aiortc 경로 한정.)
- **Video: `encode_jpeg` RGB→640×480→JPEG 코어 재사용**, 입력만 어댑트: `RGB24` 프레임 `.data`→`np.frombuffer(...).reshape(h,w,3)`→PIL→(리사이즈)→JPEG.
- **타임스탬프(poll-clock 계약)**: video=`ev.timestamp_us/1e6`(첫프레임 t0 정규화). audio=**누적 `frame.duration`**(첫프레임부터 `t += f.duration` — fallback보다 정확).

### 의사코드
```python
async def consume_video_lk(stream, w, *, target_fps=1.0, size=(640,480)):
    t0=None; emit_at=0.0
    try:
        async for ev in stream:                     # VideoFrameEvent
            rel = ev.timestamp_us/1e6
            if t0 is None: t0 = rel
            rel -= t0
            if rel + 1e-6 < emit_at: continue        # 1fps throttle (현 로직 그대로)
            emit_at = rel + 1.0/target_fps
            w.add_frame(rel, jpeg_from_rgb(ev.frame, size))   # encode_jpeg 코어 재사용
    finally:
        await stream.aclose()

async def consume_audio_lk(stream, w):              # stream=16k mono
    t=0.0
    try:
        async for ev in stream:                     # AudioFrameEvent (no ts)
            f = ev.frame
            w.add_audio(t, f.data.cast("B").tobytes())
            t += f.duration                          # 누적 미디어시계
    finally:
        await stream.aclose()
```

## 종료/루프/테스트
- aiortc `MediaStreamError` 대응물 = **EOS(`async for` 종료)**. `except MediaStreamError` 삭제. 정리는 이벤트(`disconnected` 등) + `aclose()`. `CancelledError`는 전파(현 원칙 유지).
- 단일 asyncio 루프 정합 ✅. FFI 이벤트는 백그라운드 스레드→루프 마샬링, `room.on` **콜백 안 await 금지**(ensure_future). 이벤트루프 블로킹 규칙 유지(추론은 executor, JPEG/캐스팅은 ms급 직접 OK — Slice 7 evidence).
- **SDK fake 없음** → 단위 테스트는 **async-iterable 더블 주입**(Slice 7 트랙 더블과 동일): `VideoFrameEvent(frame=fake, timestamp_us=…)`/`AudioFrameEvent(frame=fake)` yield하는 async gen. `rtc.VideoFrame(w,h,type,data)`·`rtc.AudioFrame(data,sr,nch,spc)`는 순수 파이썬 생성자(`.convert()`만 FFI → 테스트는 RGB24 가정 skip). 실 e2e는 그쪽 `infra/docker-compose.media.yml` LiveKit + publisher 스크립트(`rtc.VideoSource/AudioSource`), 서버 down이면 skip.

## "aiortc→LiveKit" 체크리스트
**바뀌는 것**: MediaStreamError import 제거→`from livekit import rtc` · `consume_video/audio`(recv 루프)→`consume_*_lk`(`async for ev`) · video ts `frame.time`→`ev.timestamp_us/1e6` · audio ts→누적 `frame.duration` · **PcmResampler/_concat_pcm/flush 제거**(16k mono 직접) · video 입력 `av.VideoFrame`→`rtc.VideoFrame`(RGB24) · 종료 예외→EOS/이벤트 · **신규 룸 lifecycle**(server 레이어).
**그대로**: `encode_jpeg` RGB→JPEG 코어 · 윈도워 `add_frame/add_audio`·`_Windower` · 1fps throttle·t0 정규화(poll-clock) · windowing/emit/recorder/sink **전부 무변경** · 이벤트루프 비차단 · media=leaf(룸 join은 media 아닌 **신규 server 레이어**에 — media는 `consume_*_lk(stream, windower)` glue까지만) · **기존 aiortc `consume_*`는 삭제하지 말고 유지**(핫패스만 LiveKit).

## Slice 8 시작점
- **deps**: `livekit~=1.1` 추가(직접 발급 시 `livekit-api~=1.1`도). `aiortc` 제거 말 것. 컨테이너 **glibc(slim) 확정**.
- **파일**: `media/ingest.py`에 `consume_video_lk`/`consume_audio_lk` + `encode_jpeg` RGB-ndarray 입력 경로 추가(코어 재사용). **신규 `server/`**: `rtc.Room()` lifecycle + `track_subscribed`/`disconnected` 핸들러 + poll 타이머(같은 미디어 타임라인) + 토큰(env `LIVEKIT_URL/API_KEY/API_SECRET` 또는 api 발급 수신; 값 로그/커밋 금지 — security.md).
- **테스트**: `test_ingest_lk.py`(async-iterable 더블로 `consume_*_lk` 직격: 타임스탬프·1fps·duration누적·PCM bytes) + 실 LiveKit e2e는 서버 게이트 skip.

출처: github.com/livekit/python-sdks · pypi.org/project/livekit · docs.livekit.io(client-sdk-js 2.19.1 / client-protocol).
