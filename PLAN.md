# giljobe v1 — WebRTC 수신 → gje(MMM) 비언어 비평 + STT 전사 → .json

## Context (왜)

`system_diagram_main.png`의 메인 루프는 **USER(webRTC: mic+webcam) → `ghe(MMM) + STT (.json)` → 면접관 LLM → SpatialReal/Elevenlabs TTS → USER**.
이 작업의 스코프는 **첫 박스 하나뿐**: 사용자의 WebRTC 웹캠+마이크 스트림을 받아 gje(MMM) 모듈로 **비언어 비평**을 내고, **STT 전사**를 수행해, 다운스트림 면접관 LLM이 먹을 **`.json`을 송출**한다. 면접관 LLM·TTS·아바타·최종평가는 전부 범위 밖.

gje는 vLLM + `google/gemma-4-E4B-it`(네이티브 AV 멀티모달) 위에서 도는 분석 사이드카. 배경 지시: **gje에서 필수적인 모듈만 giljobe로 복사해 리팩토링**(giljobe는 self-contained, gje 경로를 import하지 않음). gje 원본은 건드리지 않는다.

### 확정된 환경 사실 (탐색으로 검증)
- gje 원본 = **`/home/kio/workspace/gje/`** (복구 완료, 원위치). 포팅 소스 = `/home/kio/workspace/gje/src/local_infer/`. giljobe로는 **복사(cp)만**, 이동 금지.
- vLLM 기동 준비 완료: 도커 이미지 `vllm-gemma4-audio:local` 빌드됨, **Gemma-4-E4B 가중치 캐시됨**(`/home/kio/hf_cache`), `HF_TOKEN` 설정됨, **RTX 4090 2장 유휴**. 단, **현재 vLLM은 미기동**(8000 미응답) → 스파이크/라이브 검증 전 `bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh`로 기동(~1–2분).
- 실측용 클립: `/home/kio/workspace/gje/vid_0001.mp4`(43.5s), `/home/kio/workspace/gje/vid_0033.mp4`(50.3s) — h264 + mp3 44.1kHz 스테레오. (`video.mp4`는 오디오 없음 → STT 부적합.)
- Python 3.12.3, ffmpeg 6.1.1 존재. `aiortc/av/faster-whisper`는 미설치 → 새로 추가.

### 사용자 결정 반영
1. **실제 vLLM에 검증** (Mock 아님; 위 환경으로 기동해 실측).
2. **별도 STT 엔진 필요성부터 실측**: E4B가 오디오 네이티브이므로 같은 모델에 전사를 시켜보고 충분하면 별도 엔진 안 붙임. `Transcriber` 인터페이스 뒤에 `GemmaNativeTranscriber`(기본) / `FasterWhisperTranscriber`(폴백) 둘. → **스파이크가 1단계, 게이팅**.
3. **JSON 송출은 트레이드오프 설명 후 추천** (아래 §4).

---

## 아키텍처 (데이터 흐름)

```
브라우저 getUserMedia(웹캠+마이크)
  └─ WebRTC(aiortc) ──▶ ingest 트랙 소비자 (asyncio 이벤트루프)
        video: ~1fps로 throttle → JPEG(640×480) bytes  ┐
        audio: Opus48k → s16 16k mono PCM bytes         ┘ (타임스탬프 부착)
            └─▶ TurnWindower 버퍼 (lock 보호)
                 └─ poll(now): 윈도우 슬라이스 → 스레드풀 레인으로 submit (이벤트루프 비차단)
                      ├─ nv 레인(3s)   → WindowCritic.read_nonverbal  → NonVerbalSignal
                      ├─ stt 레인(3s)  → WindowCritic.transcribe_window → 전사 텍스트
                      └─ eval 레인(16s, 선택) → evaluate_window        → EvaluationSignal
                 worker → loop.call_soon_threadsafe(queue.put) → emit
                      └─▶ Sink: SSE GET /signals + JSONL 파일 (다운스트림 면접관 LLM 소비)
```

**동시성 핵심**: aiortc는 단일 asyncio 루프, `VllmClient.chat()`는 **블로킹**(requests ~3–7s) + `window_assembly`는 **윈도우마다 ffmpeg 서브프로세스**. 따라서 비평/STT 호출은 **반드시 이벤트루프 밖(스레드풀 레인)**에서 돌리고, 느린 eval이 프레임/오디오 수신이나 비언어/STT 레인을 막지 않도록 gje의 3레인 구조를 그대로 가져온다(`nv_exec`/`stt_exec`/`eval_exec`+`tail_exec`). worker→loop 복귀는 `loop.call_soon_threadsafe`로만.

---

## 1. 레포 구조 / 의존성

신규 앱은 giljobe 루트에 둔다. gje 원본(`/home/kio/workspace/gje`)은 건드리지 않고, 필수 모듈만 `src/giljobe/`로 복사해 리팩토링.

```
/home/kio/workspace/giljobe/
  pyproject.toml                # deps + pytest(pythonpath=["src"])
  README.md                     # 스코프, 실행법, 스파이크 결과 기록
  web/index.html                # 데모: getUserMedia → RTCPeerConnection → POST /offer
  src/giljobe/                  # ★ 계층형 subpackage (관심사별, 평면 금지). 각 폴더에 빈 __init__.py.
    config.py                   # [신규] 환경변수: VLLM_BASE_URL, STT 백엔드, 윈도우 크기, 싱크
    models/                     # 도메인 계약 (순수 데이터, giljobe 내부 의존 0)
      signals.py                # [VERBATIM 복사] NonVerbalSignal/EvaluationSignal/NONVERBAL_STATES
      records.py                # [신규] WindowRecord/turn_end 스키마 (emit가 직렬화)
    llm/                        # vLLM 전송
      vllm_client.py            # [VERBATIM] VllmClient/default_vllm_client
      vllm_stream.py            # [VERBATIM] iter_sse_delta_text
    media/                      # 미디어 인코딩·수신
      window_assembly.py        # [VERBATIM] assemble_video_url/assemble_audio_url
      ingest.py                 # [신규] aiortc 트랙 소비자: video→1fps JPEG, audio→16k mono PCM
    analysis/                   # 추론 (모델 호출; models·llm·media 의존)
      critic.py                 # [리팩토링] WindowCritic: read_nonverbal/evaluate_window + transcribe_window(신규)
      transcriber.py            # [신규] Transcriber 프로토콜 + GemmaNative/FasterWhisper
      mocks.py                  # [신규] MockCritic/MockTranscriber/SlowMockCritic (GPU 없이 테스트)
    pipeline/                   # 오케스트레이션 (analysis·media 의존)
      windowing.py              # [신규/재구현] TurnWindower: 라이브 버퍼→3s/16s 슬라이스 (gje turn_pipeline 대체)
    emit/                       # 송출 (models 의존)
      sink.py                   # [신규] Sink 프로토콜 + SSE/JSONL/Webhook (레코드 스키마는 models/records.py)
    server/                     # 앱 진입점 (전 계층 의존)
      app.py                    # [신규] aiohttp: POST /offer, GET /signals(SSE), /turn/*, 정적 web/

  # 의존 방향(단방향): server → pipeline → analysis → {models, llm, media}, emit → models.
  # Import 컨벤션: 같은 subpackage sibling=relative(`from .vllm_stream import`),
  #                다른 subpackage=absolute(`from giljobe.models.signals import`). (Slice 1에서 확정·적용)
  # YAGNI: 빈 subpackage를 미리 만들지 말 것 — 그 파일이 생기는 슬라이스에서 함께 생성.
  tests/
    test_window_assembly.py     # gje 테스트 포팅(인코딩 라운드트립/캡)
    test_windowing.py           # cadence/eot/race/레인독립 (Mock + 스레드풀)
    test_emit_schema.py         # WindowRecord JSON + nv·전사 병합
    test_ingest_slicing.py      # resample-list gotcha, 1fps throttle, PCM 정렬 (합성 PyAV 프레임)
    test_e2e_mock.py            # 합성 미디어→풀 파이프라인→JSONL (GPU/브라우저 불요)
    test_spike_transcribe.py    # ★ STT 필요성 스파이크 (실 vLLM)
    test_e2e_live.py            # vid_0001/0033 클립→실 critic (실 vLLM)
    evidence/                   # 스파이크/라이브 측정 JSON (gje .sisyphus 관례)
  scripts/serve_vllm.sh         # /home/kio/workspace/gje/serving/vllm_e4b_audio.sh 래퍼
```

**pyproject deps** (버전은 설치 시 `pip index`로 최신 안정 고정; 아래는 리서치 기준값):
```
aiortc (~=1.14)     # WebRTC asyncio
av (PyAV)           # frame.to_ndarray + AudioResampler
aiohttp             # /offer 시그널링 + SSE (aiortc 예제와 동일 스택)
numpy, Pillow       # JPEG 인코드(ndarray→JPEG; cv2 시스템 의존 회피)
requests            # VllmClient(포팅, 블로킹)
[optional] whisper = ["faster-whisper"]   # 스파이크 실패 시에만
[optional] test = ["pytest"]
```
시스템 ffmpeg 바이너리 필요(window_assembly subprocess). Python ≥3.11(=3.12 사용).

---

## 2. ★ 1단계: STT 필요성 스파이크 (게이팅)

**목적**: gemma-4-E4B가 충분히 정확히 전사하는가 → 별도 STT 엔진 생략 여부 결정.

**절차** (`tests/test_spike_transcribe.py`, 실 vLLM):
1. vLLM 기동: `HF_TOKEN=… bash /home/kio/workspace/gje/serving/vllm_e4b_audio.sh` → `curl localhost:8000/v1/models`에 `google/gemma-4-E4B-it` 뜰 때까지 대기. 하니스는 `client.health()` 게이트, 다운이면 명확히 skip(gje `test_e2e_pipeline` 관례).
2. 오디오: `/home/kio/workspace/gje/vid_0001.mp4`·`vid_0033.mp4`에서 10–16s 구간을 16k mono Int16 PCM으로 추출(`ffmpeg -ss S -t D -i clip -vn -ar 16000 -ac 1 -f s16le pipe:1`). 44.1k 스테레오→16k mono 리샘플은 라이브 경로와 동일.
3. `GemmaNativeTranscriber.transcribe_window(pcm)`로 전사: `assemble_audio_url(pcm,16000)` + 한국어 verbatim-ASR 시스템 프롬프트(§3), `temperature=0.0`, `max_tokens≈256`.
4. **GT 없음** → 자동 WER 불가. 출력/지연을 찍고 **사람이 루브릭으로 판정**: (a) 한글 verbatim인가(요약·번역·평가 아님), (b) 발화와 길이·내용 일치, (c) 환각·거부 없음, (d) 지연이 턴 cadence에 수용 가능. **두 클립×2–3 윈도우**로 과대일반화 방지. 결과를 `tests/evidence/spike-transcribe.json`에 기록.

**분기**:
- **PASS** → `GemmaNativeTranscriber`를 기본 Transcriber. **faster-whisper 안 붙임**(모델 하나, VRAM 추가 0, 네이티브 AV). README에 결정 기록.
- **FAIL**(번역/요약/환각/영어/과한 지연) → `FasterWhisperTranscriber`(CPU int8, `large-v3-turbo`, `language="ko"`)를 기본으로, `faster-whisper` deps 추가. 인터페이스 덕에 **교체는 config 한 줄**.

이 스파이크가 transcriber 기본값을 정하므로 가장 먼저. 나머지 코드는 `MockTranscriber`로 병행 진행 가능.

---

## 3. critic 리팩토링 + Transcriber

`analysis/critic.py` = gje `native_eval.WindowEvaluator`(`/home/kio/workspace/gje/src/local_infer/native_eval.py`) 리팩토링:
- `read_nonverbal(...)→NonVerbalSignal` **그대로**(채널① 비언어 비평 = 핵심 포팅), `evaluate_window(...)→EvaluationSignal` 그대로(채널②, **이 슬라이스에선 선택/부차** — 포팅은 하되 기본 핫패스 아님), `_ask_json/_extract_json/_repair_truncated_json/NONVERBAL_SYSTEM/_video_part/_audio_part/MODEL` 그대로.
- **신규 `transcribe_window(pcm, *, t, window_s, sample_rate=16000)→str`**: 주입된 `Transcriber`에 위임(critic은 STT를 박지 않음).

`analysis/transcriber.py`:
- `Transcriber` 프로토콜: `transcribe(pcm: bytes, *, sample_rate=16000) -> str`.
- `GemmaNativeTranscriber`(같은 vLLM, VRAM 추가 0): `assemble_audio_url`+`_audio_part` 재사용, plain text 반환(JSON 파싱 안 함). 한국어 ASR 시스템 프롬프트(요지): *"한국어 음성 전사기. 들리는 그대로 정확히 받아쓰되 요약·번역·해석·평가 금지. 음성 없으면 빈 문자열. JSON·설명 없이 전사 텍스트만."* `temperature=0.0`.
- `FasterWhisperTranscriber`(폴백, lazy import): `WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")`, Int16 PCM→float32 정규화→`transcribe(language="ko")`, 모델 1회 로드 재사용. CPU라 4090 2장은 vLLM 전용 유지.
- `analysis/mocks.py`: `MockCritic`(고정 NonVerbalSignal/EvaluationSignal, vLLM/ffmpeg 불요), `MockTranscriber`(고정 문자열), `SlowMockCritic`(레인 독립성 테스트용 sleep).

---

## 4. 출력 JSON 스키마 + 송출 (트레이드오프)

`models/records.py`의 `WindowRecord` — 면접관 LLM이 먹는 `.json`(`emit/sink.py`가 송출). 비언어 윈도우 1개당 1레코드, **채널① 신호 + 해당 구간 전사 병합**. 신호는 완료순(시간순 아님)으로 나오므로 레코드는 `t`로 self-describing → **소비자가 `t`로 정렬**.

```json
{ "type":"window", "session_id":"iv-…", "t":4.5, "window_s":3.0,
  "nonverbal":{"state":"hesitant","intensity":0.4,"note":"시선이 자주 아래로"},
  "transcript":"음... 제가 맡았던 역할은", "transcript_final":true }
```
턴 종료 레코드(면접관 LLM이 답변 완료를 알고 응답하도록):
```json
{ "type":"turn_end", "session_id":"…", "t":20.0,
  "transcript_full":"<t-정렬 전사 합본>",
  "eval":{ …선택 EvaluationSignal… } }
```
- 병합: nv 레인과 stt 레인을 **같은 3s 그리드**로 돌려 윈도우 인덱스로 서버에서 조인(권장). `transcript_full`(턴 끝, t-정렬 합본)이 면접관 LLM의 실제 입력, per-window 부분전사는 실시간 UX용.

**송출 트레이드오프** (소비자 = 서버측 면접관 LLM, 브라우저 아님):
| 방식 | 장점 | 단점 |
|---|---|---|
| **(a) SSE `GET /signals` + JSONL** | 표준·async 네이티브(aiohttp StreamResponse), 1:N, curl로 디버그, JSONL=리플레이/감사 로그 공짜, 다운스트림 URL에 비결합 | 소비자가 연결 유지 필요, 단방향(여기선 무방) |
| (b) WebRTC DataChannel→브라우저 | 최저지연, 미디어와 동일 전송 | **방향이 틀림**(소비자는 서버측), 브라우저 릴레이 1홉 강제, 내구성 없음 |
| (c) 다운스트림 URL로 POST 웹훅 | 푸시, 비결합, 리스너 없어도 진행 | per-window HTTP 비용, 재시도/순서/백오프 필요, URL 알아야·살아있어야, 디버그/리플레이 난해 |

**추천(v1): (a) SSE + JSONL**. 소비자가 서버측이라 (b)는 토폴로지가 틀리고, (a)는 기존 async/aiohttp 스택과 추가 인프라 0, JSONL이 테스트를 라이브 소비자 없이 assert 가능하게 함. `emit/sink.py`는 `Sink` 프로토콜로 전송 교체 가능하게 만들어, 실제 면접관 LLM URL이 생기면 (c) `WebhookSink`를 config로 추가. 데모 가시화용으로 `index.html`이 `/signals`를 EventSource로 tail.

---

## 5. 윈도잉 / 동시성 (gje turn_pipeline 재구현, giljobe 소유)

`pipeline/windowing.py`는 gje `SlidingWindowPipeline` 불변식을 그대로 유지하며 재구현:
- `add_frame(t,jpeg)`/`add_audio(t,pcm)` lock-guarded append(asyncio ingest에서 호출).
- **nv 그리드(3s)**: `poll(now)`가 `_next_nv_end`를 3.0씩 전진, `[start,start+3)` 슬라이스, `frames` 있으면 `nv_exec`에 submit. submit-후-전진을 lock 안에서 → concurrent poll에도 중복 없음.
- **stt 그리드**: nv와 동일 3s 그리드로 돌려 레코드별 nv·전사 페어링(코어스 6–8s는 config 노브; 기본 nv 정렬, turn_end가 단편 보정).
- **eval 그리드(16s, 선택)**: gje 그대로 `_covered_until`은 **연속 prefix 성공**시만 전진(실패/지연 윈도우는 end-of-turn tail이 재커버).
- **턴 경계**: `/turn/start`=새 TurnWindower(reset), `/turn/end(now)`=트레일링 부분윈도우+최종 stt 윈도우 flush, compact tail을 `tail_exec`로, `wait(timeout=eot_deadline_s)`, `turn_end` 레코드(transcript_full) emit, `close()`로 늦은 in-flight emit no-op. (데모는 RTC connect=턴 시작으로 단순화 가능.)
- worker→loop 브리지: `on_signal`이 `loop.call_soon_threadsafe(queue.put_nowait, record)`.

---

## 6. 검증 (verification)

**GPU/브라우저 불요(유닛/오프라인):**
- `pytest tests/test_window_assembly.py` — 인코딩 라운드트립·캡·빈입력 raise(ffmpeg 존재).
- `pytest tests/test_windowing.py` — cadence(3s/16s)·eot tail·실패윈도우 tail 복구·중복없음·레인독립(SlowMockCritic)·stt 레인 1윈도우 1전사.
- `pytest tests/test_emit_schema.py` — WindowRecord 형태·nv+전사 병합·turn_end t-정렬·JSONL 유효성.
- `pytest tests/test_ingest_slicing.py` — resample()가 **list** 반환 가정·1fps throttle·PCM Int16 정렬(합성 PyAV 프레임).
- `pytest tests/test_e2e_mock.py` — 합성 JPEG/PCM→TurnWindower(실 스레드풀)+Mock→JSONL 레코드수/형태·turn_end. (aiortc·모델 제외 전체 배선 증명.)

**실 vLLM(라이브):**
- vLLM 기동 → `pytest tests/test_spike_transcribe.py`(★ §2 게이팅, 두 클립).
- `pytest tests/test_e2e_live.py` — vid_0001/0033을 TurnWindower+실 WindowCritic으로 구동, per-window 비언어 지연·전사 품질·eot finalize 측정, evidence JSON. health 실패 시 skip.
- **수동 브라우저 스모크**: `http://localhost:PORT/`(localhost=secure context)에서 `web/index.html` 열어 `/offer` 협상→서버에 프레임/오디오 도착→`/signals`에 WindowRecord 표출 확인. 종료 후 `docker stop vllm-gemma4-e4b` + `nvidia-smi`로 VRAM 해제(gje GPU 위생).

---

## 7. 빌드 순서 (각 단계 독립 검증)

1. **스캐폴드 + verbatim 복사** — 레포/pyproject, `signals→models/`·`window_assembly→media/`·`vllm_client`+`vllm_stream`→`llm/`로 계층 배치 복사(import 컨벤션 §1; relative sibling 덕에 무수정). 검증: `test_window_assembly`. *GPU 불요.*
2. **★ STT 스파이크(게이팅)** — vLLM 기동, `test_spike_transcribe` 두 클립, 결정 기록. → transcriber 기본값 확정.
3. **critic 리팩토링 + transcriber + mocks** — `analysis/`에 `critic.py`(+`transcribe_window`)·`transcriber.py`(Gemma 항상; faster-whisper는 2 실패 시)·`mocks.py`. 검증: 실 vLLM에서 한국어 전사/`MockCritic` 오프라인.
4. **windowing** — `pipeline/windowing.py` 재구현. 검증: `test_windowing`(mock). *GPU 불요.*
5. **emit + 전송** — `emit/sink.py`(Sink) + `models/records.py`(WindowRecord). 검증: `test_emit_schema`. *GPU 불요.*
6. **e2e mock** — windower+mock+emit 배선. 검증: `test_e2e_mock` JSONL. *GPU/브라우저 불요.*
7. **ingest** — `media/ingest.py` aiortc 소비자(video→1fps JPEG, audio→16k PCM). 검증: `test_ingest_slicing`(합성 프레임).
8. **server + 브라우저** — `server/app.py`(/offer,/signals,/turn/*,정적), `web/index.html`. 검증: localhost 수동 스모크.
9. **라이브 e2e** — `test_e2e_live` 클립+실 critic+선택 transcriber, evidence JSON.

---

## 리스크 / 엣지

- **이벤트루프 블로킹**(최대 실패모드): critic/STT를 ingest 코루틴에서 직접 호출 금지 — 반드시 윈도워 executor 경유, worker→loop는 `call_soon_threadsafe`만.
- **Opus48k→16k resample list gotcha**: `AudioResampler.resample(frame)`는 **list 반환** — 순회·concat 안 하면 오디오 손상. `test_ingest_slicing`에서 명시 assert.
- **ffmpeg per-window 비용**: `_encode_mp4`가 윈도우(~3s)마다 서브프로세스+temp I/O. 이미 worker 스레드에서 도니 루프엔 영향 없음 — 라이브에서 누적 지연 측정, 병목일 때만 캐시/배치.
- **getUserMedia secure context**: localhost/HTTPS만 카메라·마이크. 데모는 `http://localhost` OK, 원격 호스트는 TLS 필요 — README/`index.html` 명시.
- **vLLM 라이프사이클/VRAM 위생**: 자동기동 아님 → 테스트 health 게이트·skip. 유휴 시 `docker stop` + `nvidia-smi` 확인. Gemma 전사 채택 시 STT가 모델 공유(VRAM 추가 0), faster-whisper면 CPU.
- **완료순 emit**: 레인 지연차로 순서 뒤섞임 → 소비자는 `t`로 정렬(`turn_end.transcript_full` 보정).
- **단일 세션 가정**: v1은 1 인터뷰(앱레벨 공유 executor·단일 윈도워·단일 /signals). 멀티세션은 per-session 윈도워+세션 토픽(범위 밖, 문서화).
- **전사 단편화**: 3s 그리드는 어절 중간 절단 → `transcript_full`이 면접관 LLM 입력으로 보정, per-window는 실시간용. 심하면 STT 그리드 폭을 nv와 독립적으로 확대(config).

## 범위 밖 (이 슬라이스 아님)
면접관 LLM, Elevenlabs TTS, SpatialReal 아바타, 최종평가(box⑤), gje vLLM 서빙 자체(문서만, 기동은 스크립트 호출), 멀티세션, 채널② 16s 풀 평가의 핫패스화(포팅만).
